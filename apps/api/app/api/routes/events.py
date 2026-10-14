"""Event Log query endpoints."""
import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
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


_EXPORT_FIELDS = ("id", "event_type", "entity_type", "entity_id",
                  "actor_type", "actor_id", "payload", "created_at")


def _event_to_dict(e: EventLog) -> dict:
    return {"id": e.id, "event_type": e.event_type, "entity_type": e.entity_type,
            "entity_id": e.entity_id, "actor_type": e.actor_type,
            "actor_id": e.actor_id, "payload": e.payload,
            "created_at": str(e.created_at)}


def _filtered_query(event_type=None, agent_id=None, task_id=None, limit=5000):
    """与列表接口同一套过滤条件，供导出复用。"""
    q = select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)
    if event_type:
        q = q.where(EventLog.event_type == event_type)
    if agent_id:
        q = q.where((EventLog.actor_id == agent_id)
                    | (EventLog.payload["agent_id"].as_string() == agent_id))
    if task_id:
        q = q.where((EventLog.entity_id == task_id)
                    | (EventLog.payload["task_id"].as_string() == task_id))
    return q


@router.get("/export")
async def export_events(
    format: str = Query("json", pattern="^(csv|json)$",
                        description="导出格式：csv 或 json"),
    event_type: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(5000, le=20000),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """流式导出事件日志（CSV/JSON 文件下载）。"""
    q = _filtered_query(event_type, agent_id, task_id, limit)
    rows = (await db.execute(q)).scalars().all()
    items = [_event_to_dict(e) for e in rows]

    if format == "csv":
        def _csv_stream():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(_EXPORT_FIELDS))
            writer.writeheader()
            yield buf.getvalue()
            for item in items:
                buf.seek(0)
                buf.truncate(0)
                row = dict(item)
                row["payload"] = json.dumps(row["payload"], ensure_ascii=False,
                                              default=str)
                writer.writerow(row)
                yield buf.getvalue()
        return StreamingResponse(
            _csv_stream(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="events.csv"'})

    def _json_stream():
        yield "["
        for i, item in enumerate(items):
            if i:
                yield ","
            yield json.dumps(item, ensure_ascii=False, default=str)
        yield "]"
    return StreamingResponse(
        _json_stream(), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="events.json"'})
