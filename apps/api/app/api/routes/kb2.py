"""两级知识库 API (P4)。

全局公用库 + Agent 私人库；双格式存储；模板一键导入；
跨 Agent 复制 / 公用库同步；检索策略与成本模式配置。
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, assert_company_owned,
                          assert_kb_doc_owned, get_current_user, get_db,
                          is_company_owned)
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.core.scope import ensure_agent_access
from app.core.uploads import (KB_ALLOWED_EXTENSIONS, ensure_within,
                              generate_stored_name, safe_original_name,
                              stream_upload, validate_extension)
from app.kb_engine import (COST_PROFILES, cache_invalidate,
                           cost_optimized_rag)
from app.kb_templates import SKILL_KB_TEMPLATES, list_templates
from app.knowledge_engine import _tokens
from app.models.agent import Agent
from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus
from app.models.user import User
from app.schemas.responses_kb import (KBAgentConfigOut, KBCopyOut, KBCostProfileOut,
                                      KBDocOut, KBImportOut, KBIndexedOut,
                                      KBMutationOut, KBRagPreviewOut, KBTemplateOut,
                                      KBUploadOut)

logger = get_logger(__name__)

router = APIRouter(prefix="/kb2", tags=["knowledge-p4"])


def _extract_text(raw: bytes, suffix: str) -> str:
    """按扩展名从原始字节提取文本；解析失败返回空串（调用方保留原始文件）。"""
    if suffix in (".txt", ".md", ".csv", ".json"):
        return raw.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as e:
            logger.warning("PDF 文本提取失败：%s: %s", type(e).__name__, e)
            return ""
    if suffix == ".docx":
        try:
            import io

            from docx import Document as Docx

            return "\n".join(p.text for p in Docx(io.BytesIO(raw)).paragraphs)
        except Exception as e:
            logger.warning("docx 文本提取失败：%s: %s", type(e).__name__, e)
            return ""
    return raw.decode("utf-8", errors="replace")


def _resolve_upload_dir() -> Path:
    """知识库原文上传目录。

    /tmp 重启即清空，不再使用。可配（AIEOS_KB_UPLOAD_DIR），
    默认 storage/kb-originals（与 assets.py 同一 storage 根）。
    """
    raw = os.environ.get("AIEOS_KB_UPLOAD_DIR", "").strip()
    if raw:
        base = Path(raw)
    else:
        root = Path(settings.storage_dir)
        if not root.is_absolute():
            root = Path(__file__).resolve().parents[3] / settings.storage_dir
        base = root / "kb-originals"
    base.mkdir(parents=True, exist_ok=True)
    return base


# 注意：上传目录在每次请求时用 _resolve_upload_dir() 现算，
# 不在导入期固化成常量——否则测试/运维无法在进程内改 AIEOS_KB_UPLOAD_DIR。


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _owned_agent(db: AsyncSession, user: User, agent_id: str,
                       capability: Capability = Capability.VIEW) -> Agent:
    """私人知识库的 Agent 门禁：公司角色 + 行级范围（越权 404）。

    不再手写 `companies.owner_id` 与用户 id 的等值比较：P5 之后成员身份由
    resolve_role/agent_read_scope 解析，读写分别走读/写范围。
    """
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    await ensure_agent_access(db, user, a, capability)
    return a


class DocIn(BaseModel):
    title: str
    content: str = ""
    category: str | None = None


class CopyIn(BaseModel):
    doc_ids: list[str]
    target_agent_id: str  # 目标私人库（或 "global" 导出为公用）


class SyncIn(BaseModel):
    doc_ids: list[str]  # 从公用库同步到私人


# ---------------- 文档 CRUD（带 scope 隔离）----------------

@router.post("/agents/{agent_id}/documents", response_model=KBIndexedOut)
async def create_private_doc(agent_id: str, data: DocIn,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """向 Agent 私人知识库添加文档（自动切片+摘要+关键词）。"""
    a = await _owned_agent(db, user, agent_id, Capability.OPERATE)
    doc_id = str(uuid.uuid4())
    doc = KBDocument(
        id=doc_id, company_id=a.company_id, title=data.title,
        source_type="text", content=data.content,
        scope="private", agent_id=agent_id, category=data.category,
        status=KBDocumentStatus.READY)
    # 切片 + 关键词
    chunks = _slice(data.content)
    doc.chunk_count = len(chunks)
    doc.keywords = sorted(_tokens(data.content))[:50]
    doc.summary = (data.content[:150] + "…") if len(data.content) > 150 else data.content
    db.add(doc)
    await db.flush()
    for i, ch in enumerate(chunks):
        db.add(KBChunk(id=str(uuid.uuid4()), document_id=doc_id,
                       company_id=a.company_id, seq=i,
                       content=ch, keywords=sorted(_tokens(ch))[:30]))
    await db.commit()
    cache_invalidate(a.company_id)
    return {"id": doc_id, "chunks": len(chunks)}


@router.get("/agents/{agent_id}/documents", response_model=list[KBDocOut])
async def list_private_docs(agent_id: str,
                            category: str | None = Query(None),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """列出私人库文档。"""
    a = await _owned_agent(db, user, agent_id)
    q = select(KBDocument).where(
        KBDocument.company_id == a.company_id,
        KBDocument.agent_id == agent_id, KBDocument.scope == "private")
    if category:
        q = q.where(KBDocument.category == category)
    rows = (await db.execute(q.order_by(KBDocument.created_at.desc()))).scalars().all()
    return [_doc_out(d) for d in rows]


@router.patch("/documents/{doc_id}", response_model=KBMutationOut)
async def edit_doc(doc_id: str, data: DocIn,
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """编辑文档内容（重建切片与索引）。"""
    # 归属校验：与 delete_doc 同理，只按 id 取文档会让任何登录用户改他家文档
    doc = await assert_kb_doc_owned(db, user.id, doc_id)
    doc.title = data.title or doc.title
    if data.content:
        doc.content = data.content
        # 重建切片
        old = (await db.execute(select(KBChunk).where(
            KBChunk.document_id == doc_id))).scalars().all()
        for ch in old:
            await db.delete(ch)
        chunks = _slice(data.content)
        for i, ch in enumerate(chunks):
            db.add(KBChunk(id=f"{doc_id}-{i}-{uuid.uuid4().hex[:6]}",
                           document_id=doc_id, company_id=doc.company_id,
                           seq=i, content=ch, keywords=sorted(_tokens(ch))[:30]))
        doc.chunk_count = len(chunks)
        doc.keywords = sorted(_tokens(data.content))[:50]
        doc.summary = data.content[:150]
    if data.category is not None:
        doc.category = data.category
    await db.commit()
    cache_invalidate(doc.company_id)
    return {"ok": True}


@router.delete("/documents/{doc_id}", response_model=KBMutationOut)
async def delete_doc(doc_id: str, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    # 归属校验：原先只按 id 取文档，任何登录用户都能删掉别家公司的知识库文档
    doc = await assert_kb_doc_owned(db, user.id, doc_id)
    cid = doc.company_id
    # 原始文件一并删除
    if doc.original_path and os.path.exists(doc.original_path):
        try:
            os.remove(doc.original_path)
        except OSError as rm_err:
            # 删库成功但文件残留 → 磁盘上会留下"已删除文档"的内容，
            # 属于数据泄漏面，必须留痕而不是静默
            logger.warning("删除知识库原始文件失败 path=%s: %s",
                           doc.original_path, rm_err)
    await db.delete(doc)
    await db.commit()
    cache_invalidate(cid)
    return {"ok": True}


# ---------------- 文件上传：双格式存储 ----------------

@router.post("/agents/{agent_id}/upload", response_model=KBUploadOut,
             response_model_exclude_unset=True)
async def upload_private_file(agent_id: str, file: UploadFile = File(...),
                              category: str | None = None,
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """上传文件到私人知识库：
    副本1：原始文件原样保留（original_path）
    副本2：机器可读转换——文本提取→切片→关键词→摘要
    """
    a = await _owned_agent(db, user, agent_id, Capability.OPERATE)

    # 审查 S9：原实现 `raw = await file.read()` 整文件进内存且无上限，
    # 且把用户文件名（仅替换斜杠）拼进落盘路径。现在：
    # 1) 文件名只清洗成元数据；2) 扩展名走白名单（否则 400）；
    # 3) 1MB 块流式落盘，超 Settings.max_upload_mb 立即删除半成品并 413。
    original_name = safe_original_name(file.filename)
    suffix = validate_extension(original_name, KB_ALLOWED_EXTENSIONS)
    upload_root = _resolve_upload_dir()
    # agent_id 来自路径参数，拼出目录后必须确认 resolve() 后仍在根内
    original_dir = ensure_within(upload_root, upload_root / agent_id)
    save_path, size = await stream_upload(
        file, original_dir, generate_stored_name(suffix))

    # 转换需要原始字节：此时大小已被上限约束，读回内存是有界的。
    # PDF/docx 解析是纯 CPU + 同步 IO，直接 await 函数体里会把整个事件循环
    # 卡住几十秒（期间所有请求都不响应），统一丢到线程里跑。
    import asyncio

    raw = await asyncio.to_thread(save_path.read_bytes)
    safe_name = original_name
    text = await asyncio.to_thread(_extract_text, raw, suffix)

    if not text.strip():
        # 转换失败也保留原始文件，如实标注
        doc = KBDocument(
            id=str(uuid.uuid4()), company_id=a.company_id,
            title=safe_name, source_type="file", content="",
            scope="private", agent_id=agent_id, category=category,
            original_path=str(save_path), original_name=safe_name,
            summary="(二进制文件，仅保留原始副本)",
            keywords=[], chunk_count=0,
            status=KBDocumentStatus.FAILED)
        db.add(doc)
        await db.commit()
        return {"id": doc.id, "converted": False,
                "note": "无法提取文本（二进制/不支持格式），原始文件已保留"}

    doc_id = str(uuid.uuid4())
    doc = KBDocument(
        id=doc_id, company_id=a.company_id,
        title=Path(safe_name).stem, source_type="file",
        content=text, scope="private", agent_id=agent_id,
        category=category, original_path=str(save_path),
        original_name=safe_name, status=KBDocumentStatus.READY)
    chunks = _slice(text)
    doc.chunk_count = len(chunks)
    doc.keywords = sorted(_tokens(text))[:50]
    doc.summary = text[:150] + ("…" if len(text) > 150 else "")
    db.add(doc)
    await db.flush()
    for i, ch in enumerate(chunks):
        db.add(KBChunk(id=str(uuid.uuid4()), document_id=doc_id,
                       company_id=a.company_id, seq=i,
                       content=ch, keywords=sorted(_tokens(ch))[:30]))
    await db.commit()
    cache_invalidate(a.company_id)
    return {"id": doc_id, "converted": True, "chunks": len(chunks),
            "original_saved": str(save_path), "size": size}


# ---------------- 全局公用库管理 ----------------

@router.post("/global/documents", response_model=KBIndexedOut)
async def create_global_doc(data: DocIn, company_id: str = Query(...),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """添加全局公用文档。"""
    # 归属校验：此前只认 query 里的 company_id，任何登录用户都能
    # 往他人公司的 RAG 语料里植入文档（提示词注入面）。写入需要 OPERATE。
    await assert_company_capability(db, user.id, company_id, Capability.OPERATE)
    doc_id = str(uuid.uuid4())
    doc = KBDocument(id=doc_id, company_id=company_id, title=data.title,
                     source_type="text", content=data.content,
                     scope="global", category=data.category,
                     status=KBDocumentStatus.READY)
    chunks = _slice(data.content)
    doc.chunk_count = len(chunks)
    doc.keywords = sorted(_tokens(data.content))[:50]
    doc.summary = data.content[:150]
    db.add(doc)
    await db.flush()
    for i, ch in enumerate(chunks):
        db.add(KBChunk(id=str(uuid.uuid4()), document_id=doc_id,
                       company_id=company_id, seq=i,
                       content=ch, keywords=sorted(_tokens(ch))[:30]))
    await db.commit()
    cache_invalidate(company_id)
    return {"id": doc_id, "chunks": len(chunks)}


@router.get("/global/documents", response_model=list[KBDocOut])
async def list_global_docs(company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    # 归属校验：不带校验时可直接枚举他家公司知识库文档清单
    await assert_company_owned(db, user.id, company_id)
    rows = (await db.execute(select(KBDocument).where(
        KBDocument.company_id == company_id,
        KBDocument.scope == "global").order_by(
        KBDocument.created_at.desc()))).scalars().all()
    return [_doc_out(d) for d in rows]


# ---------------- 复制 / 同步 ----------------

@router.post("/copy", response_model=KBCopyOut)
async def copy_docs(data: CopyIn, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """把文档复制到目标 Agent 私人库（doc_ids 可来自公用库或另一 Agent）。"""
    # 目标私人库必须属于调用者（sync-from-global 也经此校验）
    if data.target_agent_id != "global":
        await _owned_agent(db, user, data.target_agent_id, Capability.OPERATE)
    copied = []
    for did in data.doc_ids:
        src = await db.get(KBDocument, did)
        if not src:
            continue
        # 归属校验：不属于调用者公司的源文档按"不存在"跳过，
        # 否则可把他人文档整篇复制进自己的库（内容泄露）
        if not await is_company_owned(db, user.id, src.company_id):
            continue
        new_id = str(uuid.uuid4())
        target_agent = None if data.target_agent_id == "global" else data.target_agent_id
        new_doc = KBDocument(
            id=new_id, company_id=src.company_id,
            project_id=src.project_id, title=src.title,
            source_type=src.source_type, content=src.content,
            scope="global" if data.target_agent_id == "global" else "private",
            agent_id=target_agent, category=src.category,
            original_path=None, original_name=src.original_name,
            summary=src.summary, keywords=src.keywords,
            chunk_count=0, status=KBDocumentStatus.READY)
        # 复制切片（new_doc 先 flush 再挂 chunk）
        new_doc.chunk_count = 0
        db.add(new_doc)
        await db.flush()
        src_chunks = (await db.execute(select(KBChunk).where(
            KBChunk.document_id == did))).scalars().all()
        for i, ch in enumerate(src_chunks):
            db.add(KBChunk(id=str(uuid.uuid4()), document_id=new_id,
                           company_id=src.company_id, seq=i,
                           content=ch.content, keywords=ch.keywords))
        new_doc.chunk_count = len(src_chunks)
        copied.append({"source": did, "new_id": new_id, "title": src.title})
    await db.commit()
    cache_invalidate()
    return {"copied": copied, "count": len(copied)}


@router.post("/agents/{agent_id}/sync-from-global", response_model=KBCopyOut)
async def sync_from_global(agent_id: str, data: SyncIn,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """从公用库选择性同步文档到私人库。"""
    req = CopyIn(doc_ids=data.doc_ids, target_agent_id=agent_id)
    return await copy_docs(req, db, user)


# ---------------- 技能模板一键关联 ----------------

@router.get("/templates", response_model=list[KBTemplateOut])
async def templates():
    return list_templates()


@router.post("/agents/{agent_id}/import-template/{skill_key}", response_model=KBImportOut)
async def import_template(agent_id: str, skill_key: str,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """一键导入技能对应的知识库模板到 Agent 私人库。"""
    tpl = SKILL_KB_TEMPLATES.get(skill_key)
    if not tpl:
        raise HTTPException(404, f"template {skill_key} not found")
    a = await _owned_agent(db, user, agent_id, Capability.OPERATE)

    created = []
    for d in tpl["docs"]:
        doc_id = str(uuid.uuid4())
        doc = KBDocument(
            id=doc_id, company_id=a.company_id,
            title=d["title"], source_type="template",
            content=d["content"], scope="private", agent_id=agent_id,
            category=d["category"], skill_template=skill_key,
            status=KBDocumentStatus.READY)
        chunks = _slice(d["content"])
        doc.chunk_count = len(chunks)
        doc.keywords = sorted(_tokens(d["content"]))[:50]
        doc.summary = d["content"][:150]
        db.add(doc)
        await db.flush()
        for i, ch in enumerate(chunks):
            db.add(KBChunk(id=str(uuid.uuid4()), document_id=doc_id,
                           company_id=a.company_id, seq=i, content=ch,
                           keywords=sorted(_tokens(ch))[:30]))
        created.append({"id": doc_id, "title": d["title"]})
    await db.commit()
    cache_invalidate()
    return {"template": tpl["name"], "imported": created, "count": len(created)}


# ---------------- 检索策略 & 成本模式配置 ----------------

class KbConfigIn(BaseModel):
    kb_scope_mode: str | None = None   # private_first | global_only | private_only
    cost_mode: str | None = None       # precision | balanced | economy
    kb_top_k: int | None = None
    kb_budget_chars: int | None = None


@router.put("/agents/{agent_id}/config", response_model=KBAgentConfigOut)
async def set_kb_config(agent_id: str, data: KbConfigIn,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Agent 级检索策略 + 成本模式配置。"""
    a = await _owned_agent(db, user, agent_id, Capability.OPERATE)
    cfg = dict(a.memory_config or {})
    if data.kb_scope_mode:
        if data.kb_scope_mode not in ("private_first", "global_only", "private_only"):
            raise HTTPException(400, "invalid kb_scope_mode")
        cfg["kb_scope_mode"] = data.kb_scope_mode
    if data.cost_mode:
        if data.cost_mode not in COST_PROFILES:
            raise HTTPException(400, "invalid cost_mode")
        cfg["cost_mode"] = data.cost_mode
    if data.kb_top_k is not None:
        cfg["kb_top_k"] = max(1, min(20, data.kb_top_k))
    if data.kb_budget_chars is not None:
        cfg["kb_budget_chars"] = max(200, min(20000, data.kb_budget_chars))
    a.memory_config = cfg
    await db.commit()
    cache_invalidate(a.company_id)
    return {"memory_config": a.memory_config}


@router.get("/cost-profiles", response_model=dict[str, KBCostProfileOut])
async def cost_profiles():
    return COST_PROFILES


# ---------------- 成本优化 RAG 查询（调试用）----------------

@router.get("/agents/{agent_id}/rag", response_model=KBRagPreviewOut)
async def private_rag(agent_id: str, q: str = Query(..., min_length=1),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """成本优化的两级 RAG 检索预览。"""
    a = await _owned_agent(db, user, agent_id)
    r = await cost_optimized_rag(db, a.company_id, agent_id, q, agent=a)
    return r


# ---------------- helpers ----------------

def _slice(content: str, size: int = 400) -> list[str]:
    """语义切片：优先按段落，超长再按句切。"""
    if not content.strip():
        return []
    paras = [p.strip() for p in content.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) <= size:
            buf = f"{buf}\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            while len(p) > size:
                cut = p.rfind("。", 0, size)
                cut = cut if cut > size // 2 else size
                chunks.append(p[:cut].strip())
                p = p[cut:].strip()
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _doc_out(d: KBDocument) -> dict:
    return {"id": d.id, "title": d.title, "scope": d.scope,
            "category": d.category, "chunk_count": d.chunk_count,
            "summary": (d.summary or "")[:120],
            "has_original": bool(d.original_path),
            "original_name": d.original_name,
            "skill_template": d.skill_template,
            "created_at": str(d.created_at)}
