"""Knowledge Base engine (P2-06): chunking + keyword retrieval.

Chunking: ~500-char sliding window with 80-char overlap, split on paragraph
boundaries when possible. Retrieval: token-overlap scoring (works for CJK via
character bigrams), vector-ready for future pgvector swap.
"""
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus

CHUNK_SIZE = 500
OVERLAP = 80


def _tokens(text: str) -> set[str]:
    """中英混合分词：英文按词，中文按双字 bigram。"""
    text = text.lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", text):
        tokens.add(word)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i:i + 2])
    if len(cjk) == 1:
        tokens.add(cjk)
    return tokens


def chunk_text(content: str) -> list[str]:
    """滑窗分块，优先在段落边界切。"""
    content = content.strip()
    if not content:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", content) if p.strip()]

    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > CHUNK_SIZE:
            # 超长段落内部再切（硬切+重叠）
            start = 0
            while start < len(para):
                piece = para[start:start + CHUNK_SIZE]
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(piece)
                start += CHUNK_SIZE - OVERLAP
            continue
        if len(buf) + len(para) + 1 <= CHUNK_SIZE:
            buf = f"{buf}\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


async def ingest_document(db: AsyncSession, doc: KBDocument) -> int:
    """把文档切块落库，返回块数。"""
    try:
        pieces = chunk_text(doc.content)
        for i, piece in enumerate(pieces):
            db.add(KBChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                company_id=doc.company_id,
                seq=i,
                content=piece,
                keywords=sorted(_tokens(piece)),  # 全量存储，不截断（检索依赖）
            ))
        doc.chunk_count = len(pieces)
        doc.status = KBDocumentStatus.READY
        await db.commit()
        return len(pieces)
    except Exception:
        doc.status = KBDocumentStatus.FAILED
        await db.commit()
        raise


async def retrieve(db: AsyncSession, company_id: str, query: str,
                   top_k: int = 5, project_id: str | None = None,
                   limit: int | None = 200) -> list[dict]:
    """关键词重合度检索，返回 top_k 块。向量检索可在此替换。

    limit：在 SQL 层截断候选 chunk 数（默认 200），避免全表 chunk 拉进
    Python 做 bigram 打分导致内存爆炸；不做 pgvector 的轻方案。
    """
    q = select(KBChunk).where(KBChunk.company_id == company_id)
    if project_id:
        doc_ids_q = await db.execute(select(KBDocument.id).where(
            KBDocument.project_id == project_id))
        doc_ids = [r[0] for r in doc_ids_q.all()]
        if not doc_ids:
            return []
        q = q.where(KBChunk.document_id.in_(doc_ids))
    if limit:
        q = q.limit(limit)

    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return []

    q_tokens = _tokens(query)
    scored = []
    for r in rows:
        r_tokens = set(r.keywords or []) or _tokens(r.content)
        overlap = len(q_tokens & r_tokens)
        if overlap > 0:
            score = overlap / (len(q_tokens) ** 0.5 * len(r_tokens) ** 0.5)
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])

    return [{
        "chunk_id": r.id,
        "document_id": r.document_id,
        "seq": r.seq,
        "content": r.content,
        "score": round(score, 4),
    } for score, r in scored[:top_k]]


async def rag_context(db: AsyncSession, company_id: str, query: str,
                      top_k: int = 5, max_chars: int = 3000,
                      project_id: str | None = None,
                      limit: int | None = 200) -> str:
    """RAG 接口：检索并拼装可直接注入 prompt 的知识上下文。"""
    hits = await retrieve(db, company_id, query, top_k=top_k,
                          project_id=project_id, limit=limit)
    parts, used = [], 0
    for h in hits:
        if used + len(h["content"]) > max_chars:
            break
        parts.append(h["content"])
        used += len(h["content"])
    return "\n---\n".join(parts)
