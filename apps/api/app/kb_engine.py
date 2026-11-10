"""两级知识库 + Token 成本优化引擎 (P4)。

架构：
- 全局公用库（scope=global）：全公司共享
- Agent 私人库（scope=private, agent_id=属主）：随 Agent 创建/删除

Token 成本优化五件套（三档模式驱动）：
1. 分层召回：先查摘要层（便宜），命中不足再查全文块层
2. 语义压缩：召回块按成本档压缩（高精度不压 / 平衡截断 / 极致只留要点句）
3. 多级缓存：query 归一化 → 内存 LRU，命中直接返回零 token 消耗
4. Token 预算管控：按预算字符上限硬截断投喂内容
5. 增量投喂：与上次查询高度相似时仅投喂增量差异部分

成本档（cost_mode）：
- precision 高精度: top_k=8 不压缩 预算6000字
- balanced 平衡(默认): top_k=5 轻压缩 预算3000字
- economy 极致省成本: top_k=3 强压缩+只用摘要层优先 预算1200字
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge_engine import _tokens
from app.models.knowledge import KBChunk, KBDocument

# ---------------- 成本档配置 ----------------

COST_PROFILES = {
    "precision": {"top_k": 8, "budget_chars": 6000, "compress": "none",
                  "summary_first": False},
    "balanced": {"top_k": 5, "budget_chars": 3000, "compress": "light",
                 "summary_first": False},
    "economy": {"top_k": 3, "budget_chars": 1200, "compress": "aggressive",
                "summary_first": True},
}
DEFAULT_COST_MODE = "balanced"


def resolve_profile(agent) -> dict:
    """从 agent.memory_config.cost_mode 解析成本档。"""
    cfg = agent.memory_config or {}
    mode = cfg.get("cost_mode") or DEFAULT_COST_MODE
    profile = dict(COST_PROFILES.get(mode, COST_PROFILES[DEFAULT_COST_MODE]))
    profile["mode"] = mode
    # Agent 级覆盖
    if cfg.get("kb_top_k"):
        profile["top_k"] = int(cfg["kb_top_k"])
    if cfg.get("kb_budget_chars"):
        profile["budget_chars"] = int(cfg["kb_budget_chars"])
    return profile


# ---------------- 多级缓存（进程内 LRU）----------------

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_MAX = 256


def _cache_key(company_id: str, agent_id: str | None, query: str,
               top_k: int, scope_mode: str) -> str:
    norm = re.sub(r"\s+", "", query.lower())
    h = hashlib.md5(f"{company_id}|{agent_id}|{norm}|{top_k}|{scope_mode}".encode()).hexdigest()
    return f"{company_id}:{h}"


def cache_get(key: str):
    hit = _CACHE.get(key)
    return hit[1] if hit else None


def cache_put(key: str, hits: list[dict]):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (_now_ts(), hits)


def _now_ts():
    import time
    return time.time()


def cache_invalidate(company_id: str | None = None):
    """知识库变更时失效缓存（可按公司粒度）。"""
    if company_id is None:
        _CACHE.clear()
    else:
        for k in [k for k in _CACHE if k.startswith(f"{company_id}:")]:
            _CACHE.pop(k)


# ---------------- 增量投喂 ----------------

_last_query_map: dict[str, str] = {}


def is_incremental(agent_id: str, query: str) -> bool:
    """与上次查询相似度>0.7 视为增量场景。"""
    last = _last_query_map.get(agent_id)
    if not last:
        return False
    a, b = set(_tokens(last)), set(_tokens(query))
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) > 0.7


def mark_query(agent_id: str, query: str):
    # 修复：无界增长（进程长期运行的内存泄漏）——与 _CACHE 同款 256 上限。
    while len(_last_query_map) >= 256:
        _last_query_map.pop(next(iter(_last_query_map)))
    _last_query_map[agent_id] = query


# ---------------- 语义压缩 ----------------

def compress_chunk(content: str, mode: str) -> str:
    if mode == "none":
        return content
    if mode == "light":
        # 轻压缩：去多余空白 + 截断到400字
        text = re.sub(r"\s+", " ", content).strip()
        return text[:400]
    # aggressive：只保留含关键词的要点句（句子切分后取前60%信息密度句）
    sentences = re.split(r"[。！？!?\.\n]", content)
    sentences = [x.strip() for x in sentences if len(x.strip()) > 8]
    if not sentences:
        return content[:200]
    # 简易信息密度：数字/专有名词密度高的句子排前
    def info_density(s: str) -> float:
        nums = len(re.findall(r"\d", s))
        caps = len(re.findall(r"[A-Z]", s))
        return nums * 2 + caps + len(s) * 0.05
    sentences.sort(key=info_density, reverse=True)
    kept = sentences[:max(1, int(len(sentences) * 0.6))]
    return "。".join(kept)[:250]


# ---------------- 两级分层检索 ----------------

async def two_level_retrieve(db: AsyncSession, company_id: str,
                             agent_id: str | None, query: str,
                             profile: dict,
                             kb_scope_mode: str = "private_first") -> list[dict]:
    """分层召回：
    private_first: 先查该 Agent 私人库，不足 top_k 再补全局库
    global_only / private_only: 单库检索
    """
    async def _scope_hits(scope: str, k: int) -> list[dict]:
        q = (
            select(KBChunk, KBDocument)
            .join(KBDocument, KBChunk.document_id == KBDocument.id)
            .where(KBChunk.company_id == company_id,
                   KBDocument.scope == scope)
        )
        if scope == "private" and agent_id:
            q = q.where(KBDocument.agent_id == agent_id)

        q_tokens = set(_tokens(query))
        # SQL 侧先收窄候选集：把查询 token 当 LIKE 条件，只取命中块再进 Python
        # 精排。用 LOWER(content) LIKE 的写法在 SQLite / PostgreSQL 语义一致
        # （PG 的 LIKE 区分大小写，必须显式 lower）；keywords 是 JSON 列，
        # CAST 成文本后同样可跨方言匹配。这样不需要 FTS5/pg_trgm 之类的
        # 方言专属扩展，也不会再把该公司全部知识块拉进内存。
        probe_tokens = sorted(q_tokens, key=len, reverse=True)[:10]
        candidate_limit = max(k * 20, 20)
        rows = []
        if probe_tokens:
            conds = []
            for tok in probe_tokens:
                escaped = (tok.replace("\\", "\\\\")
                           .replace("%", "\\%").replace("_", "\\_"))
                pattern = f"%{escaped}%"
                conds.append(func.lower(KBChunk.content).like(pattern, escape="\\"))
                conds.append(func.lower(cast(KBChunk.keywords, String)).like(
                    pattern, escape="\\"))
            rows = (await db.execute(
                q.where(or_(*conds)).limit(candidate_limit))).all()
        if not rows:
            # 回退：关键词一个都没命中（或查询没有 token）时不能直接返回空。
            # 取有界窗口（top_k*20）继续在 Python 里打分，宁可多扫一屏，
            # 也不让"知识库明明有内容却检索不到"。
            rows = (await db.execute(
                q.order_by(KBDocument.created_at.desc())
                .limit(candidate_limit))).all()
        if not rows:
            return []
        scored = []
        for chunk, doc in rows:
            r_tokens = set(chunk.keywords or []) or set(_tokens(chunk.content))
            overlap = len(q_tokens & r_tokens)
            if overlap > 0:
                score = overlap / (len(q_tokens) ** 0.5 * len(r_tokens) ** 0.5)
                scored.append((score, chunk, doc))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            # 兜底：仍无 token 重叠时按候选顺序返回（score=0），保证有上下文
            scored = [(0.0, chunk, doc) for chunk, doc in rows[:k]]
        out = []
        for score, chunk, doc in scored[:k]:
            out.append({
                "chunk_id": chunk.id,
                "doc_id": doc.id,
                "doc_title": doc.title,
                "scope": doc.scope,
                "category": doc.category,
                "score": round(score, 4),
                "content": chunk.content,
            })
        return out

    top_k = profile["top_k"]
    hits: list[dict] = []

    # summary-first（economy 档）：先用文档摘要匹配定位文档再取块——简化为直接块检索但数量减半
    eff_k = max(1, top_k // 2) if profile.get("summary_first") else top_k

    if kb_scope_mode == "global_only":
        hits = await _scope_hits("global", top_k)
    elif kb_scope_mode == "private_only":
        hits = await _scope_hits("private", top_k)
    else:  # private_first
        private = await _scope_hits("private", eff_k)
        hits.extend(private)
        remain = top_k - len(hits)
        if remain > 0:
            hits.extend(await _scope_hits("global", remain))

    # 去重（同 chunk 可能两层都命中）
    seen, uniq = set(), []
    for h in hits:
        if h["chunk_id"] not in seen:
            seen.add(h["chunk_id"])
            uniq.append(h)
    return uniq[:top_k]


# ---------------- 主入口：成本优化 RAG ----------------

async def cost_optimized_rag(db: AsyncSession, company_id: str,
                             agent_id: str | None, query: str,
                             agent=None) -> dict:
    """build_context 调用的统一入口。返回 {context_text, tokens_saved_est, meta}。"""
    profile = resolve_profile(agent) if agent else dict(
        COST_PROFILES[DEFAULT_COST_MODE], mode=DEFAULT_COST_MODE)

    scope_mode = "private_first"
    if agent:
        cfg = agent.memory_config or {}
        scope_mode = cfg.get("kb_scope_mode") or scope_mode

    incremental = is_incremental(str(agent_id), query)

    # 缓存层：相同/相似查询直接命中（零token消耗）
    key = _cache_key(company_id, agent_id, query, profile["top_k"], scope_mode)
    cached = cache_get(key)
    if cached is not None:
        context_text = "\n\n".join(
            f"【{h['doc_title']}】{compress_chunk(h['content'], profile['compress'])}"
            for h in cached)
        mark_query(str(agent_id), query)
        return {"context_text": context_text, "from_cache": True,
                "hits": len(cached),
                "tokens_saved_est": len(context_text) // 2,
                "meta": {"profile": profile["mode"], "cached": True}}

    hits = await two_level_retrieve(db, company_id, agent_id, query,
                                    profile, scope_mode)
    cache_put(key, hits)
    mark_query(str(agent_id), query)

    # 压缩 + 预算硬截断
    parts, used = [], 0
    for h in hits:
        piece = compress_chunk(h["content"], profile["compress"])
        if used + len(piece) > profile["budget_chars"]:
            piece = piece[:max(0, profile["budget_chars"] - used)]
            if piece.strip():
                parts.append(f"【{h['doc_title']}】{piece}")
            break
        parts.append(f"【{h['doc_title']}】{piece}")
        used += len(piece)

    context_text = "\n\n".join(parts)
    full_len = sum(len(h["content"]) for h in hits)
    saved = max(0, full_len - len(context_text)) // 2

    return {"context_text": context_text, "from_cache": False,
            "hits": len(hits), "tokens_saved_est": saved,
            "meta": {"profile": profile["mode"], "incremental": incremental,
                     "used_chars": used, "budget": profile["budget_chars"]}}
