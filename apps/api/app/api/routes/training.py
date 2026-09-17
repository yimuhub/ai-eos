"""Training API (P3-02)."""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent
from app.models.training import (TrainingApplication, TrainingLearningPoint,
                                 TrainingProposal, TrainingSession,
                                 TrainingStatus, TrainingType)
from app.models.user import User
from app.training_engine import (apply_session, extract_learning,
                                 new_id, rollback_application)

router = APIRouter(prefix="/training", tags=["training"])


async def _agent(db: AsyncSession, agent_id: str) -> Agent:
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return a


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
    a = await _agent(db, agent_id)
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(TrainingSession).where(
        TrainingSession.agent_id == agent_id).order_by(
        TrainingSession.created_at.desc()))
    return [{"id": s.id, "title": s.title, "type": s.training_type.value,
             "status": s.status.value, "summary": s.summary,
             "created_at": s.created_at.isoformat()} for s in q.scalars().all()]


@router.get("/sessions/{sid}")
async def session_detail(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = await db.get(TrainingSession, sid)
    if not s:
        raise HTTPException(404, "not found")
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
        raise HTTPException(404, "not found")
    if s.status != TrainingStatus.EXTRACTED:
        raise HTTPException(409, f"status={s.status.value}, 只有 EXTRACTED 可批准")
    a = await _agent(db, s.agent_id)
    applied = await apply_session(db, s, a, actor=user.id)
    return {"id": sid, "status": "APPLIED", "applied_count": len(applied)}


@router.post("/sessions/{sid}/reject")
async def reject_session(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = await db.get(TrainingSession, sid)
    if not s or s.status != TrainingStatus.EXTRACTED:
        raise HTTPException(409, "只有 EXTRACTED 可拒绝")
    s.status = TrainingStatus.REJECTED
    s.decided_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).replace(tzinfo=None)
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
    """撤销一次训练应用（恢复前置状态）。"""
    app_rec = await db.get(TrainingApplication, app_id)
    if not app_rec:
        raise HTTPException(404, "not found")
    if app_rec.rolled_back_at:
        raise HTTPException(409, "已撤销过")
    s = await db.get(TrainingSession, app_rec.session_id)
    a = await _agent(db, s.agent_id)
    ok = await rollback_application(db, app_rec, a, actor=user.id)
    if not ok:
        # 空回滚（无可回滚内容）也算成功——提案状态仍需修正
        prop0 = await db.get(TrainingProposal, application.proposal_id)
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
    from fastapi import File, UploadFile
    from app.doc_convert import extract_to_md
    from app.core.config import settings as _settings

    a = await _agent(db, agent_id)
    raw = await file.read()
    fname = file.filename or "material"
    md, ok = extract_to_md(fname, raw)

    # 转换失败或需要更高质量时，用专用转换大模型整理
    if ok and md:
        try:
            conv_mid = getattr(_settings, "doc_convert_model_id", "")
            if conv_mid:
                from app.model_gateway import ModelGateway
                from app.doc_convert import convert_via_llm
                md = await convert_via_llm(md[:12000], conv_mid, ModelGateway(db))
        except Exception:
            pass  # LLM 整理失败就用原始提取结果

    # 存入私人知识库
    from app.models.knowledge import KBDocument, KBChunk, KBDocumentStatus
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
