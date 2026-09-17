"""Knowledge Base API (P2-06)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.knowledge_engine import ingest_document, rag_context, retrieve
from app.models.company import Company
from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus
from app.models.user import User

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DocCreate(BaseModel):
    title: str
    content: str
    source_type: str = "text"
    project_id: str | None = None


@router.post("/documents")
async def create_document(
    data: DocCreate,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传知识文档（自动分块入库）。"""
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    doc = KBDocument(
        id=str(uuid.uuid4()),
        company_id=company_id,
        project_id=data.project_id,
        title=data.title,
        source_type=data.source_type,
        content=data.content,
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
    q = select(KBDocument).where(KBDocument.company_id == company_id)
    if project_id:
        q = q.where(KBDocument.project_id == project_id)
    q = q.order_by(KBDocument.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    return [{
        "id": d.id, "title": d.title, "source_type": d.source_type,
        "status": d.status.value, "chunk_count": d.chunk_count,
        "project_id": d.project_id,
        "created_at": d.created_at.isoformat(),
    } for d in rows]


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await db.get(KBDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
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
    doc = await db.get(KBDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
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
    context = await rag_context(db, company_id, q, top_k=top_k,
                                max_chars=max_chars, project_id=project_id)
    return {"query": q, "context": context}
