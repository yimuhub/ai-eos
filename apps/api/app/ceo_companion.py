"""CEO 常驻助手：线程管理、今日成本与入口数据。

线程本身只存归属与滚动摘要，原文仍在 `agent_chat_messages`。
删除线程是老板显式的"清空这段记忆"，否则任何清理任务都不会动它。
"""
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_chat import AgentChatMessage
from app.models.budget import TokenBudget
from app.models.ceo_thread import CeoThread
from app.models.token_usage import TokenUsage

DEFAULT_THREAD_TITLE = "和 CEO 的对话"

QUICK_PROMPTS = [
    "今天公司有什么风险？",
    "帮我把本周目标拆成任务",
    "汇总各部门进展",
    "给我一份经营简报",
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def ensure_default_thread(db: AsyncSession, agent: Agent) -> CeoThread:
    """取该 CEO 最早的线程；没有就建一个，并把老消息归入其中。"""
    thread = (await db.execute(
        select(CeoThread).where(CeoThread.agent_id == str(agent.id))
        .order_by(CeoThread.created_at).limit(1))).scalars().first()
    if thread is None:
        thread = CeoThread(company_id=str(agent.company_id),
                           agent_id=str(agent.id), title=DEFAULT_THREAD_TITLE)
        db.add(thread)
        await db.flush()

    await db.execute(
        update(AgentChatMessage)
        .where(AgentChatMessage.agent_id == str(agent.id),
               AgentChatMessage.thread_id.is_(None))
        .values(thread_id=str(thread.id)))
    total = (await db.execute(
        select(func.count(AgentChatMessage.id))
        .where(AgentChatMessage.thread_id == str(thread.id)))).scalar_one()
    thread.message_count = int(total or 0)
    return thread


async def create_thread(db: AsyncSession, agent: Agent,
                        title: str | None = None) -> CeoThread:
    thread = CeoThread(company_id=str(agent.company_id),
                       agent_id=str(agent.id),
                       title=(title or "").strip()[:200] or "新的对话")
    db.add(thread)
    await db.flush()
    return thread


async def list_threads(db: AsyncSession, agent_id: str) -> list[CeoThread]:
    rows = (await db.execute(
        select(CeoThread).where(CeoThread.agent_id == agent_id)
        .order_by(CeoThread.last_message_at.desc().nulls_last(),
                  CeoThread.created_at.desc()))).scalars().all()
    return list(rows)


async def delete_thread(db: AsyncSession, thread: CeoThread) -> None:
    await db.execute(delete(AgentChatMessage).where(
        AgentChatMessage.thread_id == str(thread.id)))
    await db.execute(delete(CeoThread).where(CeoThread.id == str(thread.id)))


async def today_usage(db: AsyncSession, agent_id: str) -> tuple[int, float]:
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.agent_id == agent_id,
               TokenUsage.created_at >= today_start))).one()
    return int(row[0]), float(row[1])


async def budget_paused(db: AsyncSession, company_id: str,
                        agent: Agent) -> bool:
    rows = (await db.execute(
        select(TokenBudget).where(TokenBudget.company_id == company_id,
                                  TokenBudget.enabled.is_(True),
                                  TokenBudget.paused.is_(True)))).scalars().all()
    for b in rows:
        if b.scope_type == "agent" and b.scope_id != str(agent.id):
            continue
        if (b.scope_type == "department"
                and b.scope_id != str(agent.department_id or "")):
            continue
        return True
    return False


def thread_payload(thread: CeoThread) -> dict:
    return {
        "id": str(thread.id),
        "title": thread.title,
        "summary": thread.summary,
        "message_count": int(thread.message_count or 0),
        "last_message_at": (str(thread.last_message_at)
                            if thread.last_message_at else None),
        "created_at": str(thread.created_at) if thread.created_at else None,
    }


async def companion_payload(db: AsyncSession, company_id: str,
                            agent: Agent) -> dict:
    thread = await ensure_default_thread(db, agent)
    tokens, cost = await today_usage(db, str(agent.id))
    return {
        "has_ceo": True,
        "company_id": company_id,
        "ceo_agent_id": str(agent.id),
        "ceo_agent_name": agent.name,
        "ceo_role": agent.role,
        "thread_id": str(thread.id),
        "thread_title": thread.title,
        "summary": thread.summary,
        "message_count": int(thread.message_count or 0),
        "last_message_at": (str(thread.last_message_at)
                            if thread.last_message_at else None),
        "today_tokens": tokens,
        "today_cost": round(cost, 6),
        "budget_paused": await budget_paused(db, company_id, agent),
        "quick_prompts": QUICK_PROMPTS,
    }


def empty_companion_payload(company_id: str) -> dict:
    return {
        "has_ceo": False,
        "company_id": company_id,
        "ceo_agent_id": None,
        "ceo_agent_name": None,
        "ceo_role": None,
        "thread_id": None,
        "thread_title": None,
        "summary": None,
        "message_count": 0,
        "last_message_at": None,
        "today_tokens": 0,
        "today_cost": 0.0,
        "budget_paused": False,
        "quick_prompts": QUICK_PROMPTS,
    }
