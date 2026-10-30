"""Command Center: company-wide live stats."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user
from app.core.scope import build_ctx
from app.db.session import get_db
from app.models.agent import Agent, AgentStatus
from app.models.asset import Asset
from app.models.company import Company
from app.models.task import Task, TaskStatus
from app.models.event import EventLog

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# 首页"活跃任务"的定义：仍在流转中的任务（不含 TODO 之外的人工待办池）。
# bootstrap 只回这些，且 limit 20，避免前端为筛几条运行中任务而拉全量任务表。
ACTIVE_STATUSES = (TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.WAITING)


async def _get_owned_company(db: AsyncSession, user_id: str, company_id: str) -> Company:
    # 原先返回 200 + {"error": ...}：HTTP 层看不出失败，前端只能
    # 靠读 body 里的 error 字段判断，很容易漏判成"数据都是 0"。
    # 统一改成 404，与其它接口一致（前端已有 try/catch + Toast）。
    return await assert_company_owned(db, user_id, company_id)


def _task_where(ctx):
    """首页任务/执行数据的行级条件：非 owner/admin 只看自己创建的。"""
    conds = [Task.company_id == ctx.company_id]
    if not ctx.sees_all_agents:
        conds.append(Task.created_by == ctx.user_id)
    return conds


async def _dashboard_payload(db: AsyncSession, company_id: str, c: Company,
                             ctx) -> dict:
    """首页 KPI。计数全部走一次 GROUP BY，不再按状态逐条 SELECT 整行。"""
    # agent status counts：一条 GROUP BY + 一条 total
    agent_counts = {st.value.lower(): 0 for st in AgentStatus}
    for st, n in (await db.execute(
        select(Agent.status, func.count()).where(
            Agent.company_id == company_id).group_by(Agent.status)
    )).all():
        key = getattr(st, "value", st)
        agent_counts[str(key).lower()] = n
    agent_counts["total"] = (await db.execute(select(func.count()).select_from(Agent).where(
        Agent.company_id == company_id))).scalar() or 0

    # task counts：一条 GROUP BY status
    task_counts = {st.value.lower(): 0 for st in TaskStatus}
    for st, n in (await db.execute(
        select(Task.status, func.count()).where(*_task_where(ctx))
        .group_by(Task.status)
    )).all():
        key = getattr(st, "value", st)
        task_counts[str(key).lower()] = n

    active_rows = (await db.execute(
        select(Task.id, Task.title, Task.status, Task.priority,
               Task.assigned_agent_id)
        .where(*_task_where(ctx), Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.created_at.desc()).limit(10)
    )).all()
    active_tasks = [{"id": r.id, "title": r.title, "status": r.status.value,
                     "priority": r.priority.value,
                     "assigned_agent_id": r.assigned_agent_id}
                    for r in active_rows]

    completed_rows = (await db.execute(
        select(Task.id, Task.title)
        .where(*_task_where(ctx), Task.status == TaskStatus.COMPLETED)
        .order_by(Task.created_at.desc()).limit(5)
    )).all()
    completed_tasks = [{"id": r.id, "title": r.title} for r in reversed(completed_rows)]

    asset_where = [Asset.company_id == company_id]
    event_where = [EventLog.company_id == company_id]
    if not ctx.sees_all_agents:
        asset_where.append(Asset.created_by == ctx.user_id)
        event_where.append(EventLog.actor_id == ctx.user_id)
    recent_assets = (await db.execute(select(Asset).where(
        *asset_where).order_by(Asset.created_at.desc()).limit(5))).scalars().all()
    recent_events = (await db.execute(select(EventLog).where(
        *event_where).order_by(
        EventLog.created_at.desc()).limit(15))).scalars().all()

    return {
        "company": {"id": c.id, "name": c.name},
        "agents": agent_counts,
        "tasks": {**task_counts,
                  "active": active_tasks,
                  "recently_completed": completed_tasks},
        "recent_assets": [{"id": a.id, "name": a.name, "type": a.type.value}
                          for a in recent_assets],
        "recent_events": [{"event_type": e.event_type, "actor_type": e.actor_type,
                           "payload": e.payload, "created_at": str(e.created_at)}
                          for e in recent_events],
    }


async def _active_tasks_payload(db: AsyncSession, company_id: str, ctx,
                                limit: int = 20) -> list[dict]:
    """运行中任务（limit 20，只取页面需要的列，绝不带 description/result 全文）。"""
    rows = (await db.execute(
        select(Task.id, Task.title, Task.status, Task.priority,
               Task.assigned_agent_id, Task.progress, Task.current_stage,
               Task.created_at)
        .where(*_task_where(ctx), Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.created_at.desc()).limit(limit)
    )).all()
    return [{
        "id": r.id, "title": r.title, "status": r.status.value,
        "priority": r.priority.value,
        "assigned_agent_id": r.assigned_agent_id,
        "progress": r.progress,
        "current_stage": r.current_stage,
        "created_at": str(r.created_at),
    } for r in rows]


@router.get("/{company_id}")
async def dashboard(company_id: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    c = await _get_owned_company(db, user.id, company_id)
    ctx = await build_ctx(db, user, company_id)
    return await _dashboard_payload(db, company_id, c, ctx)


@router.get("/{company_id}/bootstrap")
async def dashboard_bootstrap(company_id: str, db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """首页一次性引导数据：dashboard + 部门概览 + 运行中任务。

    替代此前串行的 /dashboard、/growth-center/departments-overview、/tasks
    三个请求；轮询时只打这一个端点。
    """
    c = await _get_owned_company(db, user.id, company_id)
    ctx = await build_ctx(db, user, company_id)
    from app.api.routes.growth_center import departments_overview

    dash = await _dashboard_payload(db, company_id, c, ctx)
    # 复用成长中心实现（含归属校验与 company 过滤），避免两套聚合逻辑漂移
    overview = await departments_overview(company_id=company_id, db=db, user=user)
    active = await _active_tasks_payload(db, company_id, ctx)
    return {
        "dashboard": dash,
        "departments_overview": overview,
        "active_tasks": active,
    }
