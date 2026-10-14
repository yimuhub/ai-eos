"""Memory engine (P2-07): write/recall with importance filtering + TTL."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge_engine import _tokens
from app.models.memory import (DEFAULT_IMPORTANCE, DEFAULT_TTL, AgentMemory,
                               MemoryType)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def remember(
    db: AsyncSession,
    agent_id: str,
    company_id: str,
    memory_type: MemoryType,
    content: str,
    importance: float | None = None,
    context_ref: str | None = None,
) -> AgentMemory:
    """写入一条记忆。低于该类型最低重要度的直接丢弃。"""
    imp = importance if importance is not None else DEFAULT_IMPORTANCE[memory_type]
    if imp < DEFAULT_IMPORTANCE[memory_type] * 0.6:
        return None  # 太不重要的不记

    import uuid as _uuid
    ttl = DEFAULT_TTL[memory_type]
    mem = AgentMemory(
        id=str(_uuid.uuid4()),
        agent_id=agent_id,
        company_id=company_id,
        memory_type=memory_type,
        content=content.strip(),
        importance=imp,
        keywords=sorted(_tokens(content))[:150],
        context_ref=context_ref,
        expires_at=(_now() + timedelta(seconds=ttl)) if ttl else None,
    )
    db.add(mem)
    await db.commit()
    return mem


async def recall(db: AsyncSession, agent_id: str, query: str | None = None,
                 memory_type: MemoryType | None = None, top_k: int = 8,
                 min_importance: float = 0.0) -> list[dict]:
    """召回记忆：过滤过期 → 重要度过滤 → 关键词相关度排序。"""
    now = _now()
    q = select(AgentMemory).where(AgentMemory.agent_id == agent_id)
    if memory_type:
        q = q.where(AgentMemory.memory_type == memory_type)
    rows = (await db.execute(q)).scalars().all()

    live = [m for m in rows if m.expires_at is None or m.expires_at > now]
    live = [m for m in live if m.importance >= min_importance]

    if query:
        q_tokens = _tokens(query)
        scored = []
        for m in live:
            m_tokens = set(m.keywords or []) or _tokens(m.content)
            overlap = len(q_tokens & m_tokens)
            score = overlap / (len(q_tokens) ** 0.5 + 1e-9) + m.importance * 0.3
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        top = [m for _, m in scored[:top_k]]
    else:
        live.sort(key=lambda m: (-m.importance, -m.access_count))
        top = live[:top_k]

    # 更新访问计数
    for m in top:
        m.access_count += 1
    if top:
        await db.commit()

    return [{
        "id": m.id, "type": m.memory_type.value, "content": m.content,
        "importance": round(m.importance, 2),
        "access_count": m.access_count,
        "created_at": m.created_at.isoformat(),
        "expired_soon": bool(m.expires_at and
                             (m.expires_at - now).total_seconds() < 3600),
    } for m in top]


async def forget(db: AsyncSession, agent_id: str, memory_id: str) -> bool:
    mem = await db.get(AgentMemory, memory_id)
    if not mem or mem.agent_id != agent_id:
        return False
    await db.delete(mem)
    await db.commit()
    return True


async def cleanup_expired(db: AsyncSession, company_id: str | None = None) -> int:
    """清理过期记忆（可由调度器周期调用）。"""
    from sqlalchemy import delete
    now = _now()
    q = delete(AgentMemory).where(AgentMemory.expires_at.is_not(None),
                                  AgentMemory.expires_at <= now)
    if company_id:
        q = q.where(AgentMemory.company_id == company_id)
    result = await db.execute(q)
    await db.commit()
    return result.rowcount
