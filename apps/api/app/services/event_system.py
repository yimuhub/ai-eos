"""Unified event log — every core module reports through here."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import EventLog


async def _resolve_company_id(
    db: AsyncSession, entity_type: str | None, entity_id: str | None
) -> str | None:
    """从事件关联实体反查 company_id。

    用函数内 import 避免 models 之间的循环依赖；用 `db.get` 走主键查询，
    不会为每条事件引入全表扫描。任何异常都吞掉并返回 None——事件记录
    不能因为反查失败而丢失，但也不能把租户归属猜错（None = 不可见）。
    """
    if not entity_type or not entity_id:
        return None

    try:
        if entity_type == "task":
            from app.models.task import Task
            return await _company_of(db, Task, entity_id)
        if entity_type == "agent":
            from app.models.agent import Agent
            return await _company_of(db, Agent, entity_id)
        if entity_type == "project":
            from app.models.project import Project
            return await _company_of(db, Project, entity_id)
        if entity_type == "meeting":
            from app.models.meeting import Meeting
            return await _company_of(db, Meeting, entity_id)
        if entity_type == "kb_document":
            from app.models.knowledge import KBDocument
            return await _company_of(db, KBDocument, entity_id)
        if entity_type == "skill":
            from app.models.skill import Skill
            return await _company_of(db, Skill, entity_id)
        return None
    except Exception:  # noqa: BLE001 — 反查失败必须降级为“不可见”，不能中断业务
        return None


async def _company_of(db: AsyncSession, model, entity_id: str) -> str | None:
    """主键查询实体并取出 company_id（找不到实体则为 None）。"""
    obj = await db.get(model, entity_id)
    return getattr(obj, "company_id", None) if obj is not None else None


async def log_event(
    db: AsyncSession,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    payload: dict | None = None,
    company_id: str | None = None,
):
    # 调用方显式传入优先；否则按实体反查。反查在 db.add 之前完成，
    # 保证失败时不会留下“写了一半”的事件行。
    if company_id is None:
        company_id = await _resolve_company_id(db, entity_type, entity_id)

    db.add(
        EventLog(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
            company_id=company_id,
        )
    )
    await db.flush()
