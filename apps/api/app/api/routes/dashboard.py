"""Command Center: company-wide live stats."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent, AgentStatus
from app.models.asset import Asset
from app.models.company import Company
from app.models.task import Task, TaskStatus
from app.models.event import EventLog

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/{company_id}")
async def dashboard(company_id: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        # 原先返回 200 + {"error": ...}：HTTP 层看不出失败，前端只能
        # 靠读 body 里的 error 字段判断，很容易漏判成"数据都是 0"。
        # 统一改成 404，与其它接口一致（前端已有 try/catch + Toast）。
        raise HTTPException(404, "Company not found")

    # agent status counts
    agent_counts = {"total": 0}
    for st in AgentStatus:
        n = (await db.execute(select(func.count()).select_from(Agent).where(
            Agent.company_id == company_id, Agent.status == st))).scalar()
        agent_counts[st.value.lower()] = n
    total_agents = (await db.execute(select(func.count()).select_from(Agent).where(
        Agent.company_id == company_id))).scalar()
    agent_counts["total"] = total_agents

    # task counts
    task_counts = {}
    active_tasks, completed_tasks = [], []
    for st in TaskStatus:
        rows = (await db.execute(select(Task).where(
            Task.company_id == company_id, Task.status == st))).scalars().all()
        task_counts[st.value.lower()] = len(rows)
        if st in (TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.WAITING):
            active_tasks += [{"id": t.id, "title": t.title, "status": t.status.value,
                              "priority": t.priority.value, "assigned_agent_id": t.assigned_agent_id}
                             for t in rows]
        if st == TaskStatus.COMPLETED:
            completed_tasks = [{"id": t.id, "title": t.title} for t in rows[-5:]]

    recent_assets = (await db.execute(select(Asset).where(
        Asset.company_id == company_id).order_by(Asset.created_at.desc()).limit(5))).scalars().all()
    recent_events = (await db.execute(select(EventLog).order_by(
        EventLog.created_at.desc()).limit(15))).scalars().all()

    return {
        "company": {"id": c.id, "name": c.name},
        "agents": agent_counts,
        "tasks": {**task_counts,
                  "active": active_tasks[:10],
                  "recently_completed": completed_tasks},
        "recent_assets": [{"id": a.id, "name": a.name, "type": a.type.value}
                          for a in recent_assets],
        "recent_events": [{"event_type": e.event_type, "actor_type": e.actor_type,
                           "payload": e.payload, "created_at": str(e.created_at)}
                          for e in recent_events],
    }
