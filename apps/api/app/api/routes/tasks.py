"""Task Center + Agent execution + Pause/Resume/Cancel + Human intervention."""
import asyncio

import json
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.agent_runtime import pause_execution, run_agent_on_task, resume_execution, utcnow_iso
from app.core.logging_config import get_logger
from app.db.session import AsyncSessionLocal, get_db
from app.models.agent import Agent
from app.models.company import Company
from app.models.task import (AgentExecution, Intervention, Task, TaskEvent, TaskEventType,
                             TaskPriority, TaskStatus)
from app.services.event_system import log_event
from app.task_engine import IllegalTransition, add_task_event, transition

logger = get_logger(__name__)

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

    async def _on_timeout(session, timeout_s: float):
        # 超时先写事件告知用户（事件先落库，避免被回滚吞掉），再标 FAILED
        from app.models.task import TaskStatus as TS
        await log_event(session, "task.timeout", "task", task_id,
                        "system", None,
                        {"agent_id": agent_id, "timeout_s": timeout_s,
                         "hint": "如需更长执行时间，可调大环境变量 AIEOS_TASK_TIMEOUT_S"})
        t = await session.get(Task, task_id)
        if t and t.status == TS.RUNNING:
            t.status = TS.FAILED
        await session.commit()

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
        except Exception as exc:
            # 用结构化日志而不是 print_exc：print 走 stdout，
            # 会绕过日志级别与统一格式，线上排查时反而更难定位。
            logger.error("任务执行失败 task=%s agent=%s: %s",
                         task_id, agent_id, exc, exc_info=True)
            await session.rollback()
            t2 = await session.get(Task, task_id)
            if t2 and t2.status == TS.RUNNING:
                t2.status = TS.FAILED
            from app.models.agent import AgentStatus
            a2 = await session.get(Agent, agent_id)
            if a2 and a2.status == AgentStatus.ERROR:
                a2.status = AgentStatus.IDLE
            try:
                # 失败还必须落在**任务维度**上：用户排查时是按任务查的。
                # 只记录 agent.failed（挂在员工上）会让"这个任务为什么失败"
                # 无从检索——而这正是用户最常问的问题。
                await log_event(session, "task.failed", "task", task_id,
                                "system", None,
                                {"agent_id": agent_id,
                                 "error": f"{type(exc).__name__}: {exc}"[:800]})
                await session.commit()
            except Exception as mark_err:
                await session.rollback()
                logger.error("任务失败状态落库失败 task=%s: %s",
                             task_id, mark_err, exc_info=True)

    background_jobs.submit_async(_job, on_timeout=_on_timeout)


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


class InterventionIn(BaseModel):
    type: str  # MESSAGE|INSTRUCTION|FILE|URL|PRIORITY_CHANGE|AGENT_CHANGE
    content: str | None = None
    attachments: dict | None = {}
    new_priority: str | None = None
    new_agent_id: str | None = None


@router.post("/{tid}/takeover", status_code=201)
async def takeover_task(tid: str, data: InterventionIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """V5.5 B1 老板接管：RUNNING 任务立即暂停，写下你的改方向指令，
    恢复执行时 Agent 按你的指令继续（检查点可追溯，见执行轨迹）。"""
    t = await _task_owned(db, user.id, tid)
    if t.status not in (TaskStatus.RUNNING, TaskStatus.PLANNING):
        raise HTTPException(409, f"只有执行中的任务可以接管，当前状态：{t.status.value}")
    # 1) 暂停所有 running executions（写 execution 检查点保留现场）
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "running"))
    execs = q.scalars().all()
    for e in execs:
        await pause_execution(db, e)
    # 2) 每个被暂停的执行写 human_takeover 检查点（恢复时取出指令注入）
    from app.models.task import ExecutionCheckpoint
    note = (data.content or "老板接管：按最新要求继续").strip()
    for e in execs:
        db.add(ExecutionCheckpoint(
            execution_id=e.id, checkpoint_type="human_takeover",
            step_index=3,
            context_snapshot={"note": note[:2000], "taken_by": user.id}))
    # 3) 任务进 PAUSED
    if t.status == TaskStatus.RUNNING:
        await transition(db, t, TaskStatus.PAUSED, "user", user.id,
                         payload={"takeover": note[:200]})
    else:
        t.status = TaskStatus.PAUSED
    # 4) 干预留痕
    iv = Intervention(task_id=tid, user_id=user.id, type="TAKEOVER",
                     content=note, status="applied")
    db.add(iv)
    await db.commit()
    return {"detail": "已接管，任务暂停", "paused_executions": len(execs),
            "resume_via": f"POST /tasks/{tid}/takeover/resolve"}


@router.post("/{tid}/takeover/resolve")
async def resolve_takeover(tid: str, data: InterventionIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """V5.5 B1 接管解除：老板确认改完方向，任务从检查点恢复，
    最新接管指令作为最高优先级注入 Agent 上下文。"""
    t = await _task_owned(db, user.id, tid)
    if t.status != TaskStatus.PAUSED:
        raise HTTPException(409, f"只有已接管暂停的任务可以解除，当前状态：{t.status.value}")
    # 可选：解除前再补一条指令（恢复注入取 step_index 最大的检查点）
    if data.content:
        from app.models.task import ExecutionCheckpoint
        q = await db.execute(select(AgentExecution).where(
            AgentExecution.task_id == tid, AgentExecution.status == "paused"))
        for e in q.scalars():
            db.add(ExecutionCheckpoint(
                execution_id=e.id, checkpoint_type="human_takeover",
                step_index=4,
                context_snapshot={"note": data.content[:2000], "taken_by": user.id}))
    await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "paused"))
    for e in q.scalars():
        await resume_execution(db, e)
    return {"detail": "接管已解除，任务从检查点恢复（含老板指令）"}


@router.post("/{tid}/retry")
async def retry_task(tid: str, background: BackgroundTasks,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """失败任务重试：FAILED→PLANNING→RUNNING，重新提交后台执行。

    前端任务详情页 FAILED 状态的"重试"按钮调用此端点。
    """
    t = await _task_owned(db, user.id, tid)
    if t.status != TaskStatus.FAILED:
        raise HTTPException(409, f"只有 FAILED 状态的任务可以重试，当前状态：{t.status.value}")
    if not t.assigned_agent_id:
        raise HTTPException(400, "Task has no assigned agent")
    try:
        await transition(db, t, TaskStatus.PLANNING, "user", user.id)
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    background.add_task(_run_in_background, tid, t.assigned_agent_id)
    return {"detail": "retrying", "task_id": tid}


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
# class InterventionIn moved above its first use (takeover endpoints)


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


@router.get("/{tid}/trace")
async def task_trace(tid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """V5.5 B4 节点级执行轨迹：按时间正序的"每 Agent 每步"时间线。

    数据全来自现有表：agent_executions（每步）+ execution_checkpoints（检查点）
    + interventions（老板介入）+ token_usages（每步成本）+ event_log（事件）。
    失败可定位到具体节点。
    """
    t = await _task_owned(db, user.id, tid)
    from app.models.task import ExecutionCheckpoint, Intervention
    from app.models.token_usage import TokenUsage
    from app.models.event import EventLog
    from app.models.agent import Agent

    execs = (await db.execute(
        select(AgentExecution).where(AgentExecution.task_id == tid)
        .order_by(AgentExecution.created_at.asc()))).scalars().all()
    agent_ids = {e.agent_id for e in execs}
    agent_rows = (await db.execute(
        select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all() if agent_ids else []
    amap = {a.id: a.name for a in agent_rows}

    usage_rows = (await db.execute(
        select(TokenUsage).where(TokenUsage.task_id == tid).order_by(
            TokenUsage.created_at.asc()))).scalars().all()
    # 把用量按 agent 汇总（同 Agent 多次调用）
    usage_by_agent: dict[str, dict] = {}
    for u in usage_rows:
        k = u.agent_id or "unknown"
        slot = usage_by_agent.setdefault(k, {"tokens": 0, "cost": 0.0, "calls": 0,
                                              "models": set(), "last_model": None})
        slot["tokens"] += int(u.total_tokens or 0)
        slot["cost"] += float(u.total_cost or 0.0)
        slot["calls"] += 1
        if u.model_id:
            slot["models"].add(u.model_id)
            slot["last_model"] = u.model_id

    cps = (await db.execute(
        select(ExecutionCheckpoint).where(
            ExecutionCheckpoint.execution_id.in_(
                [e.id for e in execs] or ["__none__"]))
        .order_by(ExecutionCheckpoint.created_at.asc()))).scalars().all()
    intvs = (await db.execute(
        select(Intervention).where(Intervention.task_id == tid)
        .order_by(Intervention.created_at.asc()))).scalars().all()
    evs = (await db.execute(
        select(EventLog).where(
            or_(EventLog.entity_id == tid,
                EventLog.payload["task_id"].as_string() == tid))
        .order_by(EventLog.created_at.asc()).limit(200))).scalars().all()

    # ---- 组装时间线（正序）----
    nodes: list[dict] = []
    cp_map: dict[str, list] = {}
    for cp in cps:
        cp_map.setdefault(cp.execution_id, []).append(cp)
    for e in execs:
        cp_list = cp_map.get(e.id, [])
        meta = e.metadata_json or {}
        nodes.append({
            "kind": "execution",
            "id": e.id,
            "ts": e.created_at.isoformat() if e.created_at else None,
            "agent_id": e.agent_id,
            "agent_name": amap.get(e.agent_id, e.agent_id),
            "status": e.status,
            "input": (e.input or "")[:200],
            "output": (e.output or "")[:200],
            "error": e.error,
            "model_id": meta.get("model_id"),
            "duration_s": meta.get("duration_s"),
            "checkpoints": [
                {"type": cp.checkpoint_type, "step": cp.step_index,
                 "ts": cp.created_at.isoformat() if cp.created_at else None}
                for cp in cp_list],
            "usage": ({
                "tokens": usage_by_agent[e.agent_id]["tokens"],
                "cost": round(usage_by_agent[e.agent_id]["cost"], 4),
                "calls": usage_by_agent[e.agent_id]["calls"],
                "last_model": usage_by_agent[e.agent_id]["last_model"],
            } if e.agent_id in usage_by_agent else None),
        })
    for iv in intvs:
        # V5.6.1 事件流（学 OpenHands）：老板介入 = 流中"老板事件"，
        # 与 Agent 动作同一条时间线，可回放/审计/分叉
        nodes.append({"kind": "boss", "id": iv.id,
                     "ts": iv.created_at.isoformat() if iv.created_at else None,
                     "type": iv.type, "content": (iv.content or "")[:200],
                     "status": iv.status})
    for ev in evs:
        nodes.append({"kind": "event", "id": ev.id,
                     "ts": ev.created_at.isoformat() if ev.created_at else None,
                     "event_type": ev.event_type,
                     "detail": (str(ev.payload or {})[:150])})
    nodes.sort(key=lambda n: n.get("ts") or "")
    # 成本合计
    total = {
        "calls": sum(u["calls"] for u in usage_by_agent.values()),
        "tokens": sum(u["tokens"] for u in usage_by_agent.values()),
        "cost": round(sum(u["cost"] for u in usage_by_agent.values()), 4),
        "executions": len(execs),
        "failed": sum(1 for e in execs if e.status == "failed"),
    }
    return {"task": {"id": t.id, "title": t.title, "status": t.status},
            "nodes": nodes, "total": total}


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

    # 执行记录也是"日志"：用户真正想知道的是"我的员工做了什么、为什么失败"，
    # 而失败原因只存在于执行记录里。原实现只返回事件流与人工介入，
    # 与函数文档声称的"执行记录摘要"不符——失败任务的时间线会是空的。
    execs = (await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid).order_by(AgentExecution.created_at))).scalars().all()
    for ex in execs:
        body = ex.error or ex.output or ""
        logs.append({
            "ts": str(ex.completed_at or ex.created_at), "kind": "execution",
            "who": "AI 员工",
            "type": str(ex.status),
            "title": "执行失败" if ex.status == "failed" else "执行完成",
            "content": body[:300],
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
