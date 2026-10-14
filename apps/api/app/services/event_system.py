"""Unified event log — every core module reports through here."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import EventLog


async def log_event(
    db: AsyncSession,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    payload: dict | None = None,
):
    db.add(
        EventLog(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
        )
    )
    await db.flush()
