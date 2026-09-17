"""Event Log query endpoints."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.event import EventLog

router = APIRouter(prefix="/events", tags=["events"])


@router.get("")
async def list_events(
    agent_id: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    q = select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)
    if event_type:
        q = q.where(EventLog.event_type == event_type)
    if agent_id:
        q = q.where((EventLog.actor_id == agent_id)
                    | (EventLog.payload["agent_id"].as_string() == agent_id))
    if task_id:
        q = q.where((EventLog.entity_id == task_id)
                    | (EventLog.payload["task_id"].as_string() == task_id))
    rows = (await db.execute(q)).scalars().all()
    return [
        {"id": e.id, "event_type": e.event_type, "entity_type": e.entity_type,
         "entity_id": e.entity_id, "actor_type": e.actor_type, "actor_id": e.actor_id,
         "payload": e.payload, "created_at": str(e.created_at)}
        for e in rows
    ]
