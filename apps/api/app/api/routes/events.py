"""Event Log query endpoints."""
import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.scope import accessible_company_ids, build_ctx
from app.db.session import get_db
from app.models.agent import Agent
from app.models.event import EventLog
from app.models.membership import DepartmentMember

router = APIRouter(prefix="/events", tags=["events"])


def _accessible_company_ids(user_id: str):
    """调用者可访问的公司：owner 事实源 ∪ active 成员行。"""
    return accessible_company_ids(user_id)


def _event_scope(ctx):
    """事件行的可见条件（与用量统计同构）。

    owner/admin/超管 → 全公司；部门负责人 → 本部门成员与员工产生的事件
    ∪ 自己；普通成员 → 只看到自己的事件。事件里常带产出与指令全文，
    "能进公司"不等于"能看全员行为"。
    """
    base = EventLog.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    branches = [EventLog.actor_id == ctx.user_id]
    if ctx.head_dept_ids:
        branches.append(EventLog.actor_id.in_(
            select(DepartmentMember.user_id).where(
                DepartmentMember.department_id.in_(ctx.head_dept_ids))))
        branches.append(EventLog.actor_id.in_(
            select(Agent.id).where(Agent.department_id.in_(ctx.head_dept_ids))))
    return and_(base, or_(*branches))


async def _event_query(
    db: AsyncSession,
    user,
    *,
    company_id: str | None,
    event_type: str | None,
    agent_id: str | None,
    task_id: str | None,
    limit: int,
):
    """列表与导出共用的查询：租户过滤是**前置条件**，不是可选筛选。

    不传 company_id 时只返回可访问公司范围内的事件；传了则先解析角色。
    历史遗留的 company_id IS NULL 行一律不返回（安全优先：无法证明归属就不展示）。
    """
    q = select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)
    if company_id:
        ctx = await build_ctx(db, user, company_id)
        q = q.where(_event_scope(ctx))
    else:
        cids = (await db.execute(
            _accessible_company_ids(user.id))).scalars().all()
        scopes = [_event_scope(await build_ctx(db, user, cid)) for cid in cids]
        q = q.where(or_(*scopes) if scopes else EventLog.company_id.in_([]))

    if event_type:
        q = q.where(EventLog.event_type == event_type)
    if agent_id:
        q = q.where((EventLog.actor_id == agent_id)
                    | (EventLog.payload["agent_id"].as_string() == agent_id))
    if task_id:
        q = q.where((EventLog.entity_id == task_id)
                    | (EventLog.payload["task_id"].as_string() == task_id))
    return q


@router.get("")
async def list_events(
    agent_id: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    company_id: str | None = None,
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """按时间倒序列出事件；始终限定在调用者拥有的公司范围内。"""
    q = await _event_query(db, user, company_id=company_id,
                           event_type=event_type, agent_id=agent_id,
                           task_id=task_id, limit=limit)
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


@router.get("/export")
async def export_events(
    format: str = Query("json", pattern="^(csv|json)$",
                        description="导出格式：csv 或 json"),
    event_type: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    company_id: str | None = None,
    limit: int = Query(5000, le=5000),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """流式导出事件日志（CSV/JSON 文件下载）。

    仅导出调用者拥有的公司范围内的事件；传 company_id 时校验归属（不属于
    自己则 404）。历史 company_id 为空的行不会导出。单次最多 5000 条，
    防止审计数据被一次性全量拖走。
    """
    q = await _event_query(db, user, company_id=company_id,
                           event_type=event_type, agent_id=agent_id,
                           task_id=task_id, limit=limit)
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
