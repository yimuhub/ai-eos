"""Knowledge Base API (P2-06)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_agent_owned, assert_company_owned,
                          assert_kb_doc_owned, get_current_user, get_db)
from app.knowledge_engine import ingest_document, rag_context, retrieve
from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus
from app.models.user import User

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DocCreate(BaseModel):
    title: str
    content: str
    source_type: str = "text"
    project_id: str | None = None
    scope: str = "global"           # global=公司公用 | private=Agent 私人
    agent_id: str | None = None     # scope=private 时的属主 Agent


async def _assert_doc_scope_owned(db, user_id: str, doc: KBDocument) -> None:
    """scope=private 的文档额外校验属主 Agent 也归调用者所有。

    文档的 company_id 已由 assert_kb_doc_owned 校验；私人库还要确认
    agent_id 指向的确实是调用者自家员工，避免历史脏数据把他人 Agent 的
    私人文档挂到自家公司下被读到。
    """
    if doc.scope == "private" and doc.agent_id:
        await assert_agent_owned(db, user_id, doc.agent_id)


@router.post("/documents")
async def create_document(
    data: DocCreate,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传知识文档（自动分块入库）。"""
    # 归属校验：此前只确认公司存在，任何登录用户都能往他人公司写文档
    await assert_company_owned(db, user.id, company_id)
    if data.scope not in ("global", "private"):
        raise HTTPException(400, "scope 必须是 global 或 private")
    agent = None
    if data.scope == "private":
        if not data.agent_id:
            raise HTTPException(400, "scope=private 时必须指定 agent_id")
        agent = await assert_agent_owned(db, user.id, data.agent_id)
        if agent.company_id != company_id:
            raise HTTPException(400, "agent_id 不属于该公司")

    doc = KBDocument(
        id=str(uuid.uuid4()),
        company_id=company_id,
        project_id=data.project_id,
        title=data.title,
        source_type=data.source_type,
        content=data.content,
        scope=data.scope,
        agent_id=agent.id if agent else None,
        created_by=user.id,
    )
    db.add(doc)
    await db.commit()

    chunks = await ingest_document(db, doc)
    return {"id": doc.id, "title": doc.title, "status": doc.status.value,
            "chunk_count": chunks}


@router.get("/documents")
async def list_documents(
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 归属校验：company_id 来自调用者，不校验就等于任意租户可读
    await assert_company_owned(db, user.id, company_id)
    q = select(KBDocument).where(KBDocument.company_id == company_id)
    if project_id:
        q = q.where(KBDocument.project_id == project_id)
    q = q.order_by(KBDocument.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    return [{
        "id": d.id, "title": d.title, "source_type": d.source_type,
        "status": d.status.value, "chunk_count": d.chunk_count,
        "project_id": d.project_id, "scope": d.scope, "agent_id": d.agent_id,
        "created_at": d.created_at.isoformat(),
    } for d in rows]


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 归属校验：此前直接 db.get(KBDocument, doc_id)，知道 id 就能读别家正文
    doc = await assert_kb_doc_owned(db, user.id, doc_id)
    await _assert_doc_scope_owned(db, user.id, doc)
    chunks_q = await db.execute(select(KBChunk).where(
        KBChunk.document_id == doc_id).order_by(KBChunk.seq))
    chunks = chunks_q.scalars().all()
    return {
        "id": doc.id, "title": doc.title, "content": doc.content,
        "status": doc.status.value, "chunk_count": doc.chunk_count,
        "chunks": [{"seq": c.seq, "content": c.content[:200]} for c in chunks],
    }


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await assert_kb_doc_owned(db, user.id, doc_id)
    # 归属校验：原先只按 id 取文档，任何登录用户都能删掉别家公司的知识库文档
    await _assert_doc_scope_owned(db, user.id, doc)
    await db.delete(doc)
    await db.commit()
    return {"detail": "deleted"}


@router.get("/search")
async def search(
    company_id: str = Query(...),
    q: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """知识检索：返回最相关的块。"""
    # 归属校验：company_id 由调用者指定，不校验即可检索任何公司的知识库
    await assert_company_owned(db, user.id, company_id)
    hits = await retrieve(db, company_id, q, top_k=top_k, project_id=project_id)
    return {"query": q, "results": hits}


@router.get("/rag")
async def rag(
    company_id: str = Query(...),
    q: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    max_chars: int = Query(3000, ge=200, le=8000),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """RAG 接口：返回可直接注入 prompt 的上下文文本。"""
    # 归属校验：RAG 会把他人公司正文拼进 prompt，泄露面比列表更大
    await assert_company_owned(db, user.id, company_id)
    context = await rag_context(db, company_id, q, top_k=top_k,
                                max_chars=max_chars, project_id=project_id)
    return {"query": q, "context": context}
