"""Knowledge Base engine (P2-06): chunking + keyword retrieval.

Chunking: ~500-char sliding window with 80-char overlap, split on paragraph
boundaries when possible. Retrieval: token-overlap scoring (works for CJK via
character bigrams), vector-ready for future pgvector swap.

V6 M1-f：接入混合检索（向量语义 + 关键词 bigram）。向量路径是**增强**而
非依赖——公司没配 embedding 模型、上游调用失败、维度不一致，行为都完全
退回原关键词检索（app/kb_embedding.py 负责这些判断，本模块只消费结果）。
"""
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import kb_embedding
from app.core.logging_config import get_logger
from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus

logger = get_logger(__name__)

CHUNK_SIZE = 500
OVERLAP = 80

# 混合检索权重（V6 M1-f 定版）：语义主导、关键词兜底的简单线性混合。
# 两个分都已归一到 0~1（关键词分天然 ≤1，余弦按 (cos+1)/2 映射），
# 权重才有共同尺度。后续要调，只动这两个常数。
VECTOR_WEIGHT = 0.6
KEYWORD_WEIGHT = 0.4
# 向量路径启用时把 SQL 候选池从 200 提到 500：关键词分 >0 才入围，200 够；
# 语义相关的块关键词分可能是 0，必须让它们有机会进 Python 打分环节。
VECTOR_CANDIDATE_LIMIT = 500


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
    """把文档切块落库，返回块数。

    切块后尝试整批 embed（一次调用拿全部 chunk 向量）；无配置/调用失败
    时 embedding=None 照常入库——摄入永远不该被向量服务拖住。
    """
    try:
        pieces = chunk_text(doc.content)
        vectors = None
        if pieces:
            try:
                vectors = await kb_embedding.embed_texts(
                    db, doc.company_id, pieces)
            except Exception as exc:  # noqa: BLE001 — embed_texts 约定不抛，兜底
                logger.warning("知识库摄入 embed 失败（不影响入库）：%s: %s",
                               type(exc).__name__, str(exc)[:200])
                vectors = None
        for i, piece in enumerate(pieces):
            db.add(KBChunk(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                company_id=doc.company_id,
                seq=i,
                content=piece,
                embedding=vectors[i] if vectors and i < len(vectors) else None,
                keywords=sorted(_tokens(piece)),  # 全量存储，不截断（检索依赖）
            ))
        doc.chunk_count = len(pieces)
        doc.status = KBDocumentStatus.READY
        await db.commit()
        return len(pieces)
    except Exception:
        # 先回滚：切块/写块中途失败时，已 add 的半批 chunk 必须一起撤销，
        # 否则会话处于待回滚状态，紧接着的 commit 会抛 PendingRollbackError
        # 掩盖真正的失败原因。id 必须在 rollback 前取出——rollback 会让 ORM
        # 对象过期，之后再读属性会触发异步惰性加载并抛 MissingGreenlet。
        doc_id = doc.id
        await db.rollback()
        failed = await db.get(KBDocument, doc_id)
        if failed is not None:
            failed.status = KBDocumentStatus.FAILED
            await db.commit()
        raise


async def retrieve(db: AsyncSession, company_id: str, query: str,
                   top_k: int = 5, project_id: str | None = None,
                   limit: int | None = 200,
                   visible_agent_ids: set[str] | None = None) -> list[dict]:
    """混合检索：向量语义分与关键词 bigram 分线性混合，返回 top_k 块。

    返回结构与纯关键词时代完全一致（chunk_id/document_id/seq/content/score）。

    打分（V6 M1-f 定版）：

    * chunk 有可用向量（维度与查询向量一致）：
      ``score = 0.6 * cosine01 + 0.4 * keyword``（cosine01 = (cos+1)/2）；
    * 无可用向量的块退回纯关键词分；关键词零重合且无向量的块不入围——
      与历史行为一致；
    * 关键词零重合但**有**向量的块凭语义分入围：这正是向量路径的意义
      （换个说法、用词完全不同也能命中）。

    降级链：公司没配 embedding 模型 / 公司从未有向量入库（省一次 API 调用）/
    查询 embed 失败 / 维度不一致 → 全部退回纯关键词打分，结果与旧版一致。

    limit：在 SQL 层截断候选 chunk 数，避免全表 chunk 拉进 Python 打分导致
    内存爆炸；向量路径启用时提到 500（语义相关的块关键词分可能是 0，候选
    池太小它们进不了打分环节）。
    候选必须显式排序：没有 ORDER BY 时 SQLite 返回的是存储顺序的任意子集，
    知识库一大，真正相关的块可能根本进不了候选集，检索会随数据增长悄悄变差。
    这里取「最新入库优先」作为确定性兜底，同分时靠 sort 稳定性保持该次序。
    """
    q = select(KBChunk).join(
        KBDocument, KBChunk.document_id == KBDocument.id).where(
        KBChunk.company_id == company_id)
    # 修复：scope=private 的块此前对全公司任意角色可检索（与 kb2 两级库
    # 的隔离设计不一致）。visible_agent_ids=None = 不过滤（引擎内部调用方
    # 传 None）；用户检索入口必须传可见 Agent 集合。
    if visible_agent_ids is not None:
        if visible_agent_ids:
            q = q.where(or_(KBDocument.scope == "global",
                            KBDocument.agent_id.in_(visible_agent_ids)))
        else:
            q = q.where(KBDocument.scope == "global")
    if project_id:
        doc_ids_q = await db.execute(select(KBDocument.id).where(
            KBDocument.project_id == project_id))
        doc_ids = [r[0] for r in doc_ids_q.all()]
        if not doc_ids:
            return []
        q = q.where(KBChunk.document_id.in_(doc_ids))

    # 向量路径：查询也要 embed。三道闸省掉无意义调用——空查询、
    # 公司从未有向量入库（比如配置 embedding 模型前就入库的老数据）、
    # embed 失败（embed_texts 返回 None）。任何一闸没过都退回纯关键词。
    qvec: list[float] | None = None
    if query.strip():
        has_any_vector = (await db.execute(
            select(KBChunk.id).where(
                KBChunk.company_id == company_id,
                KBChunk.embedding.is_not(None)).limit(1))).first()
        if has_any_vector:
            vecs = await kb_embedding.embed_texts(db, company_id, [query])
            if vecs and vecs[0]:
                qvec = vecs[0]
    if qvec is not None and limit and limit < VECTOR_CANDIDATE_LIMIT:
        limit = VECTOR_CANDIDATE_LIMIT

    q = q.order_by(KBChunk.created_at.desc(), KBChunk.seq.asc())
    if limit:
        q = q.limit(limit)

    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return []

    q_tokens = _tokens(query)
    q_root = len(q_tokens) ** 0.5
    scored = []
    for r in rows:
        r_tokens = set(r.keywords or []) or _tokens(r.content)
        overlap = len(q_tokens & r_tokens)
        vec = kb_embedding.usable_vector(r.embedding, qvec)
        if overlap > 0:
            keyword = overlap / (q_root * len(r_tokens) ** 0.5)
            if vec is not None:
                score = (VECTOR_WEIGHT
                         * kb_embedding.cosine_similarity(qvec, vec)
                         + KEYWORD_WEIGHT * keyword)
            else:
                score = keyword
            scored.append((score, r))
        elif vec is not None:
            # 关键词完全对不上、语义对上了：混合检索存在的理由。
            scored.append((VECTOR_WEIGHT
                           * kb_embedding.cosine_similarity(qvec, vec), r))
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
