"""Training API (P3-02)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_agent_owned, get_current_user, get_db
from app.core.config import settings
from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.training import (TrainingApplication, TrainingLearningPoint,
                                 TrainingProposal, TrainingSession,
                                 TrainingStatus, TrainingType)
from app.models.user import User
from app.training_engine import (apply_session, extract_learning,
                                 new_id, rollback_application)

logger = get_logger(__name__)

router = APIRouter(prefix="/training", tags=["training"])


async def _agent(db: AsyncSession, agent_id: str, user_id: str) -> Agent:
    """取 Agent 并校验归属。

    改造说明：原实现只判断 Agent 是否存在，**没有任何归属校验**。
    这是一个典型的 IDOR：任何登录用户只要猜到一个 agent_id，
    就能替他人公司的 Agent 创建训练、读取/批准其训练内容。
    现在所有入口都必须传入当前用户，由 assert_agent_owned 统一把关。
    """
    return await assert_agent_owned(db, user_id, agent_id)


class SessionIn(BaseModel):
    training_type: TrainingType
    title: str
    raw_input: str | None = None
    source_url: str | None = None


@router.post("/sessions", status_code=201)
async def create_session(
    data: SessionIn,
    agent_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建训练会话并立即用 LLM 提取学习点。"""
    a = await _agent(db, agent_id, user.id)
    s = TrainingSession(id=new_id(), agent_id=a.id, company_id=a.company_id,
                        training_type=data.training_type, title=data.title,
                        raw_input=data.raw_input, source_url=data.source_url,
                        created_by=user.id)
    db.add(s)
    await db.commit()
    try:
        extraction = await extract_learning(db, s)
    except Exception as e:
        raise HTTPException(502, f"提取失败: {e}")
    return {"id": s.id, "status": s.status.value, **extraction}


@router.get("/sessions")
async def list_sessions(
    agent_id: str = Query(...),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出某 Agent 的训练会话。

    改造说明：新增归属校验；并支持可选 status 过滤——
    前端原本就在传 `status=APPLIED`，但后端未声明该参数，
    FastAPI 默认忽略未知查询参数，导致"按状态筛选"静默失效（拿到全部）。
    """
    a = await _agent(db, agent_id, user.id)
    q = select(TrainingSession).where(TrainingSession.agent_id == a.id)
    if status:
        q = q.where(TrainingSession.status == status)
    rows = (await db.execute(
        q.order_by(TrainingSession.created_at.desc()))).scalars().all()
    return [{"id": s.id, "title": s.title, "type": s.training_type.value,
             "status": s.status.value, "summary": s.summary,
             "created_at": s.created_at.isoformat()} for s in rows]


@router.get("/sessions/{sid}")
async def session_detail(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = await db.get(TrainingSession, sid)
    if not s:
        raise HTTPException(404, "训练会话不存在")
    await _agent(db, s.agent_id, user.id)  # 归属校验
    lq = await db.execute(select(TrainingLearningPoint).where(
        TrainingLearningPoint.session_id == sid))
    pq = await db.execute(select(TrainingProposal).where(
        TrainingProposal.session_id == sid))
    apps = await db.execute(select(TrainingApplication).where(
        TrainingApplication.session_id == sid))
    return {
        "id": s.id, "title": s.title, "type": s.training_type.value,
        "status": s.status.value, "summary": s.summary,
        "raw_input": (s.raw_input or "")[:500],
        "learning_points": [{"content": p.content, "category": p.category}
                            for p in lq.scalars().all()],
        "proposals": [{"id": p.id, "change_type": p.change_type,
                       "payload": p.payload, "status": p.status}
                      for p in pq.scalars().all()],
        "applications": [{"id": a.id, "applied_changes": a.applied_changes,
                          "rolled_back": bool(a.rolled_back_at)}
                         for a in apps.scalars().all()],
    }


@router.post("/sessions/{sid}/approve")
async def approve_session(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户批准 → 应用全部提案。"""
    s = await db.get(TrainingSession, sid)
    if not s:
        raise HTTPException(404, "训练会话不存在")
    a = await _agent(db, s.agent_id, user.id)  # 归属校验
    if s.status != TrainingStatus.EXTRACTED:
        raise HTTPException(409, f"当前状态为 {s.status.value}，只有已提取学习点（EXTRACTED）的会话可批准")
    applied = await apply_session(db, s, a, actor=user.id)
    return {"id": sid, "status": "APPLIED", "applied_count": len(applied)}


@router.post("/sessions/{sid}/reject")
async def reject_session(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = await db.get(TrainingSession, sid)
    # 改造：区分"不存在"(404) 与"状态不对"(409)。原实现把两者都返回 409，
    # 用户看到的是"只有 EXTRACTED 可拒绝"而不是"会话不存在"，排障时会误判。
    if not s:
        raise HTTPException(404, "训练会话不存在")
    await _agent(db, s.agent_id, user.id)  # 归属校验
    if s.status != TrainingStatus.EXTRACTED:
        raise HTTPException(409, "只有已提取学习点的会话可拒绝")
    s.status = TrainingStatus.REJECTED
    s.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    pq = await db.execute(select(TrainingProposal).where(
        TrainingProposal.session_id == sid))
    for p in pq.scalars().all():
        p.status = "REJECTED"
    await db.commit()
    return {"id": sid, "status": "REJECTED"}


@router.post("/applications/{app_id}/rollback")
async def rollback(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销一次训练应用（恢复前置状态）。

    修复三处缺陷：
    1. `application.proposal_id` → 未定义名 `application`（应为 app_rec）→ NameError → 500。
    2. 原代码完全没有租户归属校验：任何登录用户知道 app_id 就能撤销他人公司的训练。
    3. `s = await db.get(TrainingSession, ...)` 可能为 None，随后 `s.agent_id` 抛
       AttributeError，被错误地表现为 500 而非明确的 404。
    """
    app_rec = await db.get(TrainingApplication, app_id)
    if not app_rec:
        raise HTTPException(404, "训练应用记录不存在")
    session = await db.get(TrainingSession, app_rec.session_id)
    if not session:
        raise HTTPException(404, "训练会话不存在")
    # 归属校验：会话所属 Agent 必须属于当前用户自己的公司
    agent = await assert_agent_owned(db, user.id, session.agent_id)

    if app_rec.rolled_back_at:
        raise HTTPException(409, "该次训练已撤销过，无需重复操作")

    ok = await rollback_application(db, app_rec, agent, actor=user.id)
    if not ok:
        # 空回滚（无可回滚内容）也算成功——提案状态仍需修正
        prop0 = await db.get(TrainingProposal, app_rec.proposal_id)
        if prop0:
            prop0.status = "REJECTED"
        await db.commit()
    return {"id": app_id, "rolled_back": True}


@router.post("/upload-material")
async def upload_training_material(
    agent_id: str = Query(...),
    company_id: str = Query(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """上传训练资料附件 → 自动转 MD → 存入该 Agent 私人知识库（供学习）。"""
    import uuid as _uuid
    from pathlib import Path

    from app.doc_convert import extract_to_md

    a = await _agent(db, agent_id, user.id)
    # company_id 是必填查询参数，但真正可信的来源是 Agent 自身所属公司；
    # 两者不一致说明调用方传错了（或试图跨租户写入），明确拒绝而不是静默按 agent 走。
    if company_id != a.company_id:
        raise HTTPException(400, "company_id 与目标 Agent 所属公司不一致")

    raw = await file.read()
    # 上传体积上限：避免超大文件把内存和知识库拖垮（原实现无任何限制）
    MAX_UPLOAD_BYTES = 20 * 1024 * 1024
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"文件过大（{len(raw) // 1024 // 1024}MB），上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")

    fname = file.filename or "material"
    md, ok = extract_to_md(fname, raw)

    # 转换失败或需要更高质量时，用专用转换大模型整理
    if ok and md:
        try:
            conv_mid = getattr(settings, "doc_convert_model_id", "")
            if conv_mid:
                from app.doc_convert import convert_via_llm
                from app.model_gateway import ModelGateway
                md = await convert_via_llm(md[:12000], conv_mid, ModelGateway(db))
        except Exception as conv_err:
            # 不再静默：转换增强失败时说明原因，原始提取结果仍然可用
            logger.warning("训练资料 LLM 整理失败，改用原始提取结果：%s", conv_err,
                           exc_info=True)

    # 存入私人知识库
    from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus
    doc_id = str(_uuid.uuid4())
    safe_title = Path(fname).stem
    text = md if (ok and md) else ""
    doc = KBDocument(
        id=doc_id, company_id=a.company_id, title=f"[训练] {safe_title}",
        source_type="file", content=text, scope="private",
        agent_id=agent_id, original_name=fname,
        summary=text[:150], chunk_count=0, status=KBDocumentStatus.READY)
    db.add(doc)
    await db.flush()
    # 简单切片入库
    chunks = [text[i:i + 800] for i in range(0, len(text), 800) if text[i:i + 800].strip()]
    for i, ch in enumerate(chunks):
        db.add(KBChunk(id=str(_uuid.uuid4()), document_id=doc_id,
                       company_id=a.company_id, seq=i, content=ch))
    doc.chunk_count = len(chunks)
    await db.commit()
    return {"ok": True, "converted": bool(ok and md), "chunks": len(chunks),
            "doc_id": doc_id, "title": f"[训练] {safe_title}"}
