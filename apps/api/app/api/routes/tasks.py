"""Task Center + Agent execution + Pause/Resume/Cancel + Human intervention."""
import asyncio

import json
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.agent_runtime import pause_execution, run_agent_on_task, resume_execution, utcnow_iso
from app.db.session import AsyncSessionLocal, get_db
from app.models.agent import Agent
from app.models.company import Company
from app.models.task import (AgentExecution, Intervention, Task, TaskEvent, TaskEventType,
                             TaskPriority, TaskStatus)
from app.services.event_system import log_event
from app.task_engine import IllegalTransition, add_task_event, transition

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskIn(BaseModel):
    company_id: str
    title: str
    description: str | None = None
    priority: str = "NORMAL"
    assigned_agent_id: str | None = None
    parent_task_id: str | None = None


async def _task_owned(db: AsyncSession, user_id: str, task_id: str) -> Task:
    t = await db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    c = await db.get(Company, t.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Task not found")
    return t


@router.get("")
async def list_tasks(company_id: str | None = None, status: str | None = None,
                     agent_id: str | None = None,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = select(Task).join(Company, Company.id == Task.company_id).where(
        Company.owner_id == user.id)
    if company_id:
        q = q.where(Task.company_id == company_id)
    if status:
        q = q.where(Task.status == TaskStatus(status))
    if agent_id:
        q = q.where(Task.assigned_agent_id == agent_id)
    rows = (await db.execute(q.order_by(Task.created_at.desc()))).scalars().all()
    return [{c.key: getattr(t, c.key) for c in t.__table__.columns} for t in rows]


@router.post("", status_code=201)
async def create_task(data: TaskIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    payload = data.model_dump()
    payload.pop("priority", None)
    t = Task(**payload, status=TaskStatus.TODO,
             priority=TaskPriority(data.priority), created_by=user.id)
    db.add(t)
    await db.flush()
    await add_task_event(db, t.id, TaskEventType.CREATED, "user", user.id,
                         {"title": data.title})
    await log_event(db, "task.created", "task", t.id, "user", user.id, {"title": data.title})
    await db.commit()
    await db.refresh(t)
    return {c2.key: getattr(t, c2.key) for c2 in t.__table__.columns}


@router.get("/{tid}")
async def get_task(tid: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    d = {c.key: getattr(t, c.key) for c in t.__table__.columns}
    # subtasks + executions + events for full trace view
    subs = await db.execute(select(Task).where(Task.parent_task_id == tid))
    d["subtasks"] = [s.id for s in subs.scalars()]
    execs = await db.execute(select(AgentExecution).where(AgentExecution.task_id == tid))
    d["executions"] = [
        {"id": e.id, "agent_id": e.agent_id, "status": e.status,
         "output": e.output, "error": e.error}
        for e in execs.scalars()]
    events = []
    from app.models.task import TaskEvent as TE
    ev_rows = await db.execute(select(TE).where(TE.task_id == tid).order_by(TE.created_at))
    d["events"] = [
        {"type": ev.event_type.value, "actor_type": ev.actor_type,
         "payload": ev.payload, "at": str(ev.created_at)}
        for ev in ev_rows.scalars()]
    inter = await db.execute(select(Intervention).where(Intervention.task_id == tid))
    d["interventions"] = [
        {"id": i.id, "type": i.type, "content": i.content, "status": i.status}
        for i in inter.scalars()]
    return d


def _run_in_background(task_id: str, agent_id: str):
    """Run agent in a worker thread with a fresh engine (asyncpg is loop-bound)."""
    import app.background_jobs as background_jobs

    async def _job(session):
        agent = await session.get(Agent, agent_id)
        task = await session.get(Task, task_id)
        from app.models.task import TaskStatus as TS
        try:
            await run_agent_on_task(session, agent, task, actor_type="system")
            if task.status != TS.COMPLETED:
                await transition(session, task, TS.COMPLETED,
                                 actor_type="agent", actor_id=agent_id)
            else:
                await session.commit()
        except Exception:
            import traceback
            traceback.print_exc()
            await session.rollback()
            t2 = await session.get(Task, task_id)
            if t2 and t2.status == TS.RUNNING:
                t2.status = TS.FAILED
            from app.models.agent import AgentStatus
            a2 = await session.get(Agent, agent_id)
            if a2 and a2.status == AgentStatus.ERROR:
                a2.status = AgentStatus.IDLE
            await session.commit()

    background_jobs.submit_async(_job)


@router.post("/{tid}/start")
async def start_task(tid: str, background: BackgroundTasks,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    if not t.assigned_agent_id:
        raise HTTPException(400, "Task has no assigned agent")
    try:
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    background.add_task(_run_in_background, tid, t.assigned_agent_id)
    return {"detail": "started", "task_id": tid}


@router.post("/{tid}/pause")
async def pause_task(tid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    try:
        await transition(db, t, TaskStatus.PAUSED, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    # persist checkpoint on any running execution
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "running"))
    for e in q.scalars():
        await pause_execution(db, e)
    return {"detail": "paused"}


@router.post("/{tid}/resume")
async def resume_task(tid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    try:
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "paused"))
    for e in q.scalars():
        await resume_execution(db, e)
    return {"detail": "resumed"}


@router.post("/{tid}/cancel")
async def cancel_task(tid: str, reason: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    try:
        await transition(db, t, TaskStatus.CANCELLED, "user", user.id,
                         payload={"reason": reason})
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid,
        AgentExecution.status.in_(["running", "paused"])))
    for e in q.scalars():
        e.status = "cancelled"
        e.completed_at = utcnow_iso()
    await db.commit()
    return {"detail": "cancelled"}


# ---------- Human In The Loop (P1-15) ----------
class InterventionIn(BaseModel):
    type: str  # MESSAGE|INSTRUCTION|FILE|URL|PRIORITY_CHANGE|AGENT_CHANGE
    content: str | None = None
    attachments: dict | None = {}
    new_priority: str | None = None
    new_agent_id: str | None = None


@router.post("/{tid}/interventions", status_code=201)
async def add_intervention(tid: str, data: InterventionIn,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid)
    iv = Intervention(task_id=tid, user_id=user.id, type=data.type,
                      content=data.content, attachments=data.attachments,
                      status="delivered")
    db.add(iv)
    await add_task_event(db, tid, TaskEventType.INTERVENTION, "user", user.id,
                         {"type": data.type, "content": (data.content or "")[:500]})
    await log_event(db, "intervention.added", "task", tid, "user", user.id,
                    {"type": data.type})
    applied = {}
    if data.new_priority and data.type == "PRIORITY_CHANGE":
        t.priority = TaskPriority(data.new_priority)
        applied["priority"] = data.new_priority
    if data.new_agent_id and data.type == "AGENT_CHANGE":
        t.assigned_agent_id = data.new_agent_id
        applied["assigned_agent_id"] = data.new_agent_id
    await db.commit()
    await db.refresh(iv)
    return {"id": iv.id, "status": iv.status, "applied": applied}


@router.get("/{tid}/executions")
async def list_executions(tid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    await _task_owned(db, user.id, tid)
    q = await db.execute(select(AgentExecution).where(AgentExecution.task_id == tid))
    return [{c.key: getattr(e, c.key) for c in e.__table__.columns} for e in q.scalars()]


@router.get("/{tid}/logs")
async def task_logs(tid: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """任务日志：事件流 + 人工介入 + 执行记录摘要，按时间倒序。"""
    t = await _task_owned(db, user.id, tid)
    from app.models.task import Intervention as IV
    from app.models.task import TaskEvent as TE

    logs: list[dict] = []

    evs = (await db.execute(select(TE).where(TE.task_id == tid).order_by(
        TE.created_at))).scalars().all()
    for e in evs:
        logs.append({
            "ts": str(e.created_at), "kind": "event", "who": "system",
            "type": e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type),
            "title": "", 
            "content": json.dumps(e.payload, ensure_ascii=False)[:300] if e.payload else "",
        })

    ivs = (await db.execute(select(IV).where(IV.task_id == tid).order_by(
        IV.created_at))).scalars().all()
    for i in ivs:
        logs.append({
            "ts": str(i.created_at), "kind": "intervention",
            "who": "老板（人工介入）",
            "type": i.type.value if hasattr(i.type, "value") else str(i.type),
            "title": "", "content": i.content or "",
        })

    logs.sort(key=lambda x: x["ts"], reverse=True)
    return {"count": len(logs), "logs": logs}


class TaskPatchIn(BaseModel):
    assigned_agent_id: str | None = None
    priority: str | None = None
    description: str | None = None


@router.patch("/{tid}")
async def patch_task(tid: str, data: TaskPatchIn,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """更新任务可编辑字段（负责人/优先级/描述）。记录审计事件。"""
    t = await _task_owned(db, user.id, tid)
    changes = {}
    if "assigned_agent_id" in data.model_fields_set:
        t.assigned_agent_id = data.assigned_agent_id
        changes["assigned_agent_id"] = data.assigned_agent_id
    if data.priority is not None:
        t.priority = data.priority
        changes["priority"] = data.priority
    if data.description is not None:
        t.description = data.description
        changes["description"] = data.description
    if changes:
        db.add(TaskEvent(task_id=tid, event_type=TaskEventType.ASSIGNED,
                         actor_type="user", actor_id=user.id, payload=changes))
        await db.commit()
    await db.refresh(t)
    return {c.key: getattr(t, c.key) for c in t.__table__.columns}
