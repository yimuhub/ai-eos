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

from app.api.deps import get_current_user, get_db
from app.core.logging_config import get_logger
from app.kb_engine import (COST_PROFILES, cache_invalidate,
                           cost_optimized_rag)
from app.kb_templates import SKILL_KB_TEMPLATES, list_templates
from app.knowledge_engine import _tokens
from app.models.agent import Agent
from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/kb2", tags=["knowledge-p4"])

UPLOAD_DIR = Path("/tmp/ai-eos-kb-originals")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _owned_agent(db: AsyncSession, user_id: str, agent_id: str) -> Agent:
    from app.models.company import Company
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    c = await db.get(Company, a.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Agent not found")
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

@router.post("/agents/{agent_id}/documents")
async def create_private_doc(agent_id: str, data: DocIn,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """向 Agent 私人知识库添加文档（自动切片+摘要+关键词）。"""
    a = await _owned_agent(db, user.id, agent_id)
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


@router.get("/agents/{agent_id}/documents")
async def list_private_docs(agent_id: str,
                            category: str | None = Query(None),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """列出私人库文档。"""
    await _owned_agent(db, user.id, agent_id)
    q = select(KBDocument).where(
        KBDocument.agent_id == agent_id, KBDocument.scope == "private")
    if category:
        q = q.where(KBDocument.category == category)
    rows = (await db.execute(q.order_by(KBDocument.created_at.desc()))).scalars().all()
    return [_doc_out(d) for d in rows]


@router.patch("/documents/{doc_id}")
async def edit_doc(doc_id: str, data: DocIn,
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """编辑文档内容（重建切片与索引）。"""
    doc = await db.get(KBDocument, doc_id)
    if not doc:
        raise HTTPException(404)
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


@router.delete("/documents/{doc_id}")
async def delete_doc(doc_id: str, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    doc = await db.get(KBDocument, doc_id)
    if not doc:
        raise HTTPException(404)
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

@router.post("/agents/{agent_id}/upload")
async def upload_private_file(agent_id: str, file: UploadFile = File(...),
                              category: str | None = None,
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """上传文件到私人知识库：
    副本1：原始文件原样保留（original_path）
    副本2：机器可读转换——文本提取→切片→关键词→摘要
    """
    a = await _owned_agent(db, user.id, agent_id)

    raw = await file.read()
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    original_dir = UPLOAD_DIR / agent_id
    original_dir.mkdir(parents=True, exist_ok=True)
    save_path = original_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    save_path.write_bytes(raw)  # 原始格式保留

    # 机器可读转换：按类型提取文本
    suffix = Path(safe_name).suffix.lower()
    text = ""
    if suffix in (".txt", ".md", ".csv", ".json"):
        text = raw.decode("utf-8", errors="replace")
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as e:
            text = ""
            extract_note = f"PDF 解析失败: {e}"
        else:
            extract_note = None
    elif suffix in (".docx",):
        try:
            from docx import Document as Docx
            import io
            dx = Docx(io.BytesIO(raw))
            text = "\n".join(p.text for p in dx.paragraphs)
        except Exception:
            text = ""
    else:
        # 尝试当纯文本读
        text = raw.decode("utf-8", errors="replace")

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
            "original_saved": str(save_path), "size": len(raw)}


# ---------------- 全局公用库管理 ----------------

@router.post("/global/documents")
async def create_global_doc(data: DocIn, company_id: str = Query(...),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """添加全局公用文档。"""
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


@router.get("/global/documents")
async def list_global_docs(company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    rows = (await db.execute(select(KBDocument).where(
        KBDocument.company_id == company_id,
        KBDocument.scope == "global").order_by(
        KBDocument.created_at.desc()))).scalars().all()
    return [_doc_out(d) for d in rows]


# ---------------- 复制 / 同步 ----------------

@router.post("/copy")
async def copy_docs(data: CopyIn, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """把文档复制到目标 Agent 私人库（doc_ids 可来自公用库或另一 Agent）。"""
    copied = []
    for did in data.doc_ids:
        src = await db.get(KBDocument, did)
        if not src:
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


@router.post("/agents/{agent_id}/sync-from-global")
async def sync_from_global(agent_id: str, data: SyncIn,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """从公用库选择性同步文档到私人库。"""
    req = CopyIn(doc_ids=data.doc_ids, target_agent_id=agent_id)
    return await copy_docs(req, db, user)


# ---------------- 技能模板一键关联 ----------------

@router.get("/templates")
async def templates():
    return list_templates()


@router.post("/agents/{agent_id}/import-template/{skill_key}")
async def import_template(agent_id: str, skill_key: str,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """一键导入技能对应的知识库模板到 Agent 私人库。"""
    tpl = SKILL_KB_TEMPLATES.get(skill_key)
    if not tpl:
        raise HTTPException(404, f"template {skill_key} not found")
    a = await _owned_agent(db, user.id, agent_id)

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


@router.put("/agents/{agent_id}/config")
async def set_kb_config(agent_id: str, data: KbConfigIn,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Agent 级检索策略 + 成本模式配置。"""
    a = await _owned_agent(db, user.id, agent_id)
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


@router.get("/cost-profiles")
async def cost_profiles():
    return COST_PROFILES


# ---------------- 成本优化 RAG 查询（调试用）----------------

@router.get("/agents/{agent_id}/rag")
async def private_rag(agent_id: str, q: str = Query(..., min_length=1),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """成本优化的两级 RAG 检索预览。"""
    a = await _owned_agent(db, user.id, agent_id)
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
