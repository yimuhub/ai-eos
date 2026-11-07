"""Task Center + Agent execution + Pause/Resume/Cancel + Human intervention."""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_agent_owned, assert_company_capability,
                          get_current_user)
from app.agent_runtime import pause_execution, run_agent_on_task, resume_execution, utcnow_iso
from app.background_jobs import QUEUE_FULL
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.core.scope import build_ctx
from app.db.session import get_db
from app.models.agent import Agent
from app.models.automation import AutomationDefinition
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.queue_item import QueueItemStatus
from app.models.task import (AgentExecution, Intervention, Task, TaskEvent, TaskEventType,
                             TaskPriority, TaskStatus)
from app.models.user import User
from app.schemas.responses import (LogsOut, TaskActionOut,
                                   TaskDetailOut, TaskExecutionOut,
                                   TaskInterventionAddedOut, TaskOut,
                                   TaskStartedOut, TaskSummaryOut,
                                   TaskTakeoverOut, TaskTemplateOut)
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


def _accessible_company_ids(user_id: str):
    """调用者可访问的公司：owner 事实源 ∪ active 成员行。"""
    owned = select(Company.id).join(
        User, User.id == Company.owner_id).where(User.id == user_id)
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user_id,
        CompanyMember.status == "active")
    return select(Company.id).where(
        or_(Company.id.in_(owned), Company.id.in_(member)))


def _task_scope(ctx):
    """任务行的可见条件：owner/admin 看全公司，其余只看自己创建的。

    Task 没有部门字段，无法做部门级收窄——按"创建者"这一最小可用口径
    限制，宁可让成员少看到一些任务，也不把别人的执行结果/失败原因摊开。
    """
    base = Task.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    return and_(base, Task.created_by == ctx.user_id)


async def _task_owned(db: AsyncSession, user_id: str, task_id: str,
                      capability: Capability = Capability.VIEW) -> Task:
    t = await db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    await assert_company_capability(db, user_id, t.company_id, capability)
    user = await db.get(User, user_id)
    ctx = await build_ctx(db, user, t.company_id)
    if not ctx.sees_all_agents and t.created_by != user_id:
        raise HTTPException(404, "Task not found")
    return t


@router.get("", response_model=list[TaskOut])
async def list_tasks(company_id: str | None = None, status: str | None = None,
                     agent_id: str | None = None,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    if company_id:
        q = select(Task).where(_task_scope(await build_ctx(db, user, company_id)))
    else:
        cids = (await db.execute(
            _accessible_company_ids(user.id))).scalars().all()
        scopes = [_task_scope(await build_ctx(db, user, cid)) for cid in cids]
        q = select(Task).where(or_(*scopes) if scopes
                               else Task.company_id.in_([]))
    if status:
        try:
            status_enum = TaskStatus(status)
        except ValueError:
            raise HTTPException(400, f"未知的任务状态：{status}（可选：todo/planning/running/paused/completed/failed/cancelled）")
        q = q.where(Task.status == status_enum)
    if agent_id:
        q = q.where(Task.assigned_agent_id == agent_id)
    rows = (await db.execute(q.order_by(Task.created_at.desc()))).scalars().all()
    return [{c.key: getattr(t, c.key) for c in t.__table__.columns} for t in rows]


@router.post("", status_code=201, response_model=TaskOut)
async def create_task(data: TaskIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    # 建任务 = 日常写操作（OPERATE）：成员可以，只读不行
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.OPERATE)
    # 被指派的员工也必须属于该公司（且属于调用者）：此前只校验 company，
    # 带他人公司的 agent_id 即可把任务挂到别家员工身上
    if data.assigned_agent_id:
        agent = await assert_agent_owned(db, user.id, data.assigned_agent_id)
        if agent.company_id != data.company_id:
            raise HTTPException(404, "Agent not found")
    payload = data.model_dump()
    payload.pop("priority", None)
    try:
        priority = TaskPriority(data.priority)
    except ValueError:
        raise HTTPException(400, "priority 必须是 LOW/NORMAL/HIGH/URGENT 之一")
    t = Task(**payload, status=TaskStatus.TODO,
             priority=priority, created_by=user.id)
    db.add(t)
    await db.flush()
    await add_task_event(db, t.id, TaskEventType.CREATED, "user", user.id,
                         {"title": data.title})
    await log_event(db, "task.created", "task", t.id, "user", user.id, {"title": data.title})
    await db.commit()
    await db.refresh(t)
    return {c2.key: getattr(t, c2.key) for c2 in t.__table__.columns}


# 任务模板复用现有 automation_definitions 表（不再造第二套模板表）：
# 一条模板 = mode=TEST + status=PAUSED 的自动化定义，steps 首步 create_task。
_TASK_TEMPLATE_KIND = "TASK_TEMPLATE"


class SaveAsTemplateIn(BaseModel):
    name: str | None = None
    description: str | None = None


@router.post("/{tid}/save-as-template", response_model=TaskTemplateOut)
async def save_task_as_template(tid: str, data: SaveAsTemplateIn | None = None,
                                db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    """把已完成任务沉淀为可复用模板（仅 COMPLETED）。

    幂等约定：同公司同名的**任务模板**重复保存时返回已有模板（200），
    不新建第二行；若同名的是普通自动化规则则 409，避免语义混淆。
    模板复用 AutomationDefinition（mode=TEST / status=PAUSED），
    因此会出现在自动化页，可被克隆或改为定时触发。
    """
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    if t.status != TaskStatus.COMPLETED:
        raise HTTPException(
            409, f"只有已完成任务可以存为模板，当前状态：{t.status.value}")

    body = data or SaveAsTemplateIn()
    name = ((body.name or t.title or "").strip() or "任务模板")[:200]
    desc = body.description if body.description is not None else t.description

    same_name = (await db.execute(select(AutomationDefinition).where(
        AutomationDefinition.company_id == t.company_id,
        AutomationDefinition.name == name))).scalars().all()
    for d in same_name:
        if (d.trigger_config or {}).get("kind") == _TASK_TEMPLATE_KIND:
            return {"id": d.id, "name": d.name}
    if same_name:
        raise HTTPException(409, f"同名自动化规则已存在：{name}")

    params: dict = {"title": t.title}
    if desc:
        params["description"] = desc
    if t.priority is not None:
        params["priority"] = getattr(t.priority, "value", str(t.priority))
    if t.project_id:
        params["project_id"] = t.project_id
    if t.assigned_agent_id:
        params["assigned_agent_id"] = t.assigned_agent_id

    d = AutomationDefinition(
        id=str(uuid.uuid4()), company_id=t.company_id, name=name,
        trigger_type="MANUAL",
        trigger_config={"kind": _TASK_TEMPLATE_KIND,
                        "source_task_id": t.id,
                        "source_task_title": t.title},
        steps=[{"seq": 1, "type": "create_task", "params": params}],
        mode="TEST", status="PAUSED", max_runs_per_day=10,
        attachments=[], created_by=user.id)
    db.add(d)
    await log_event(db, "task.saved_as_template", "task", t.id, "user", user.id,
                    {"template_id": d.id, "name": name})
    await db.commit()
    return {"id": d.id, "name": d.name}


@router.get("/{tid}", response_model=TaskDetailOut)
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
         "output": e.output, "error": e.error,
         "tools_used": (e.metadata_json or {}).get("tools_used") or []}
        for e in execs.scalars()]
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


@router.get("/{tid}/summary", response_model=TaskSummaryOut)
async def get_task_summary(tid: str, db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """轮询专用轻量视图：去掉 result 全文，execution 输出截断到 2KB。

    归属校验直接复用 `_task_owned`（与 GET /tasks/{id} 完全同一条守卫），
    避免两套校验逻辑漂移。
    """
    t = await _task_owned(db, user.id, tid)
    # Task 全列里唯一可能上万字的是 result；summary 不返回它（首屏详情已拉过）
    d = {c.key: getattr(t, c.key) for c in t.__table__.columns if c.key != "result"}

    subs = await db.execute(select(Task.id).where(Task.parent_task_id == tid))
    d["subtasks"] = list(subs.scalars())

    execs = (await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid).order_by(AgentExecution.created_at))).scalars().all()
    output_limit = 2048
    d["executions"] = [
        {"id": e.id, "agent_id": e.agent_id, "status": e.status,
         "output": (e.output or "")[:output_limit] if e.output else e.output,
         "error": (e.error or "")[:output_limit] if e.error else e.error,
         "output_truncated": bool(e.output and len(e.output) > output_limit),
         "tools_used": (e.metadata_json or {}).get("tools_used") or []}
        for e in execs]
    d["executions_total"] = len(execs)

    from app.models.task import TaskEvent as TE
    # 事件流只保留最近 100 条（按时间正序回传，前端直接渲染）
    ev_rows = (await db.execute(
        select(TE).where(TE.task_id == tid)
        .order_by(TE.created_at.desc()).limit(100))).scalars().all()
    d["events"] = [
        {"type": ev.event_type.value, "actor_type": ev.actor_type,
         "payload": ev.payload, "at": str(ev.created_at)}
        for ev in reversed(ev_rows)]

    inter = await db.execute(select(Intervention).where(Intervention.task_id == tid))
    d["interventions"] = [
        {"id": i.id, "type": i.type, "content": i.content, "status": i.status}
        for i in inter.scalars()]
    return d


# 任务执行期间队列条目的租约时长（秒）。每 30s 续租一次，
# 因此租约 120s 足够覆盖"续租线程短暂卡住"，又能在崩溃后 2 分钟内被回收。
TASK_JOB_LEASE_S = 120


def _task_job_payload(task_id: str, agent_id: str) -> dict:
    return {"task_id": task_id, "agent_id": agent_id}


def _run_in_background(task_id: str, agent_id: str,
                       queue_item_id: str | None = None):
    """Run agent in a worker thread with a fresh engine (asyncpg is loop-bound).

    queue_item_id 非空时，执行前先 CAS 领取队列条目：同一条目被重复投递
    （启动自愈 + 手动重试）时只有一个 worker 能执行，不会重复计费。
    """
    import asyncio

    import app.background_jobs as background_jobs
    from app.services import job_queue

    async def _on_timeout(session, timeout_s: float):
        # 超时先写事件告知用户（事件先落库，避免被回滚吞掉），再标 FAILED
        from sqlalchemy import update as _update

        from app.models.task import TaskStatus as TS
        await log_event(session, "task.timeout", "task", task_id,
                        "system", None,
                        {"agent_id": agent_id, "timeout_s": timeout_s,
                         "hint": "如需更长执行时间，可调大环境变量 AIEOS_TASK_TIMEOUT_S"})
        # CAS：只有仍处于 RUNNING 才落 FAILED，不覆盖期间被用户取消/暂停的状态
        res = await session.execute(
            _update(Task).where(Task.id == task_id, Task.status == TS.RUNNING)
            .values(status=TS.FAILED)
            .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            logger.info("任务超时但状态已被并发修改（未标 FAILED）task=%s", task_id)
        else:
            # 同事务补一条失败通知：超时也是"失败"，用户不该只在任务列表里发现
            from app.services.notification_service import notify_task_terminal
            t = await session.get(Task, task_id)
            if t is not None:
                await notify_task_terminal(
                    session, t, "FAILED", error=f"执行超时（{timeout_s}s）")
        await session.commit()

    async def _mark_queue(session, fn, *args):
        """队列状态更新是"记账"，失败只记日志，不能掩盖任务本身的执行结果。"""
        if not queue_item_id:
            return
        try:
            await fn(session, queue_item_id, *args)
        except Exception as q_err:  # noqa: BLE001
            logger.error("队列条目状态更新失败 task=%s item=%s: %s",
                         task_id, queue_item_id, q_err, exc_info=True)

    async def _job(session):
        keeper = None
        if queue_item_id:
            # 领取失败说明这一轮已由别的 worker 开始执行：直接退出，
            # 保证"同一条任务只跑一次"（重复执行=重复计费）。
            claimed = await job_queue.claim_item(
                session, queue_item_id, job_queue.WORKER_ID,
                lease_s=TASK_JOB_LEASE_S)
            if claimed is None:
                logger.info("队列条目不可领取，跳过重复执行 task=%s item=%s",
                            task_id, queue_item_id)
                return
            keeper = job_queue.LeaseKeeper(
                queue_item_id, job_queue.WORKER_ID, lease_s=TASK_JOB_LEASE_S)
            keeper.start()
        try:
            await _execute_task(session)
            await _mark_queue(session, job_queue.complete)
        except asyncio.CancelledError:
            # 超时看门狗会取消本协程：条目必须离开 RUNNING，
            # 否则要等租约过期才会被启动自愈回收。
            await _mark_queue(session, job_queue.fail, "执行超时或被中断")
            raise
        finally:
            if keeper is not None:
                keeper.stop()

    async def _execute_task(session):
        agent = await session.get(Agent, agent_id)
        task = await session.get(Task, task_id)
        from app.models.task import TaskStatus as TS
        try:
            await run_agent_on_task(session, agent, task, actor_type="system")
            if task.status != TS.COMPLETED:
                try:
                    await transition(session, task, TS.COMPLETED,
                                     actor_type="agent", actor_id=agent_id)
                except IllegalTransition as conflict:
                    # 执行期间任务被老板取消/暂停：尊重最新状态，不硬转 COMPLETED。
                    # 仍把执行结果提交落库（证据保留），但不改写任务状态。
                    logger.info("任务状态已被并发修改，不再置 COMPLETED task=%s: %s",
                                task_id, conflict)
                    await session.commit()
            else:
                await session.commit()
        except Exception as exc:
            # 用结构化日志而不是 print_exc：print 走 stdout，
            # 会绕过日志级别与统一格式，线上排查时反而更难定位。
            logger.error("任务执行失败 task=%s agent=%s: %s",
                         task_id, agent_id, exc, exc_info=True)
            await session.rollback()
            t2 = await session.get(Task, task_id)
            failed_now = False
            if t2 and t2.status == TS.RUNNING:
                t2.status = TS.FAILED
                failed_now = True
            if failed_now:
                # 与 log_event 同事务：任务真的转成 FAILED 时才通知创建人
                from app.services.notification_service import notify_task_terminal
                await notify_task_terminal(
                    session, t2, "FAILED",
                    error=f"{type(exc).__name__}: {exc}")
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
            await _mark_queue(session, job_queue.fail,
                              f"{type(exc).__name__}: {exc}")

    # 返回投递结果：QUEUE_FULL 时调用方（task_runner）要把任务退回 TODO，
    # 否则会留下"RUNNING 但没有 worker 在跑"的僵尸行。
    return background_jobs.submit_async(_job, on_timeout=_on_timeout)


async def _dispatch_or_revert(db: AsyncSession, task_id: str, agent_id: str,
                              company_id: str | None = None,
                              revert_to: TaskStatus = TaskStatus.TODO) -> None:
    """投递后台执行；队列满则把刚置的 RUNNING 退回原状态并回 503。

    原先用 `background.add_task(...)` 丢弃了返回值：worker 池满时任务已经是
    RUNNING 却没有任何 worker，前端只能一直转圈，用户也取消不掉预期结果。

    revert_to 是进入 RUNNING 之前的状态（TODO 或 PAUSED）：修复前一律退回
    TODO，从暂停恢复的任务队满后被写成 TODO，丢失暂停语义且无 auto_start 标记。

    投递前先把执行意图写进持久队列：进程重启后，没跑完的任务由启动自愈
    重新投递，而不是永远停在 RUNNING。
    """
    import app.background_jobs as background_jobs
    from app.services import job_queue

    item, created = await job_queue.enqueue_detailed(
        db, kind=job_queue.TASK_EXEC_KIND, company_id=company_id,
        dedupe_key=job_queue.task_dedupe_key(task_id),
        payload=_task_job_payload(task_id, agent_id),
        # 执行失败不自动重跑：同一任务重跑会重复消耗模型额度，
        # 失败后由用户显式点「重试」决定（见 POST /tasks/{tid}/retry）。
        max_attempts=1)
    # 必须先提交：后台线程用独立 engine，读不到未提交的队列行
    await db.commit()
    if not created and item.status == QueueItemStatus.RUNNING:
        # 已有 worker 正在跑同一条目：幂等返回，不再启动第二次执行
        logger.info("任务已在执行中，跳过重复投递 task=%s item=%s",
                    task_id, item.id)
        return

    submitted = _run_in_background(task_id, agent_id, queue_item_id=item.id)
    if submitted is background_jobs.QUEUE_FULL:
        # RUNNING→TODO 不在状态机白名单里（正常流程没有"回退"语义），
        # 这是投递失败的补偿，用条件 UPDATE 而不是 transition。
        await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
            .values(status=revert_to)
            .execution_options(synchronize_session=False))
        # 排队条目也要撤掉：否则同键去重会让下次启动误判"已有活跃条目"，
        # 而 auto_start 的调度循环会重新入队（下一拍再来）。
        await job_queue.cancel(db, item.id)
        await db.commit()
        raise HTTPException(503, "后台执行队列已满，请稍后重试")
    await db.commit()


@router.post("/{tid}/start", response_model=TaskStartedOut)
async def start_task(tid: str,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    if not t.assigned_agent_id:
        raise HTTPException(400, "Task has no assigned agent")
    # 先结束只读事务（此处无待写数据）：SQLite/WAL 下"读快照 → 升级写"会直接
    # SQLITE_BUSY_SNAPSHOT，先 commit 让下面的抢占从一个干净写事务开始。
    await db.commit()
    # CAS 抢占：双击/重试/双端并发时只有一个请求能把 TODO/PAUSED 改成 RUNNING，
    # 也就只有一个请求会投递后台执行 —— 不会重复执行、重复计费。
    prev_status = t.status
    try:
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    await _dispatch_or_revert(db, tid, t.assigned_agent_id, t.company_id,
                              revert_to=prev_status)
    return {"detail": "started", "task_id": tid}


@router.post("/{tid}/pause", response_model=TaskActionOut)
async def pause_task(tid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    await db.commit()
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


@router.post("/{tid}/resume", response_model=TaskActionOut)
async def resume_task(tid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    await db.commit()
    # 并发点击「继续」时只有一次能 PAUSED→RUNNING 成功；resume_execution
    # 会提交新的后台执行，所以必须由 CAS 保证只提交一次。
    try:
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    # 只恢复最新一个 paused 执行；其余的批量置 superseded——一次任务可能积累
    # 多个 paused 执行，全部恢复会并发重跑同一任务（双份计费、结果互相覆盖）。
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "paused")
        .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()))
    execs = list(q.scalars())
    if not execs:
        # 没有 paused 执行：任务可能从未真正开跑（首跑前就被暂停），
        # 或全部执行已终态。按冷启动重新投递（与 start 同路径，含队满回退），
        # 而不是留一个 RUNNING 无 worker 的僵尸。
        await _dispatch_or_revert(db, tid, t.assigned_agent_id, t.company_id,
                                  revert_to=TaskStatus.PAUSED)
        return {"detail": "resumed"}
    for stale in execs[1:]:
        await db.execute(
            update(AgentExecution)
            .where(AgentExecution.id == stale.id,
                   AgentExecution.status == "paused")
            .values(status="superseded", completed_at=utcnow_iso())
            .execution_options(synchronize_session=False))
    await db.commit()
    _, submit_result = await resume_execution(db, execs[0])
    if submit_result == QUEUE_FULL:
        # 队满：把任务回滚到 PAUSED，执行保持 paused（旧执行已 superseded，
        # 用户再点一次「继续」会重新走本路径恢复最新执行）
        try:
            await transition(db, t, TaskStatus.PAUSED, "user", user.id)
            await db.commit()
        except IllegalTransition:
            await db.rollback()
        raise HTTPException(503, "后台执行队列已满，任务已回到暂停状态，请稍后重试")
    return {"detail": "resumed"}


class InterventionIn(BaseModel):
    type: str  # MESSAGE|INSTRUCTION|FILE|URL|PRIORITY_CHANGE|AGENT_CHANGE
    content: str | None = None
    attachments: dict | None = {}
    new_priority: str | None = None
    new_agent_id: str | None = None


@router.post("/{tid}/takeover", status_code=201,
            response_model=TaskTakeoverOut)
async def takeover_task(tid: str, data: InterventionIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """V5.5 B1 老板接管：RUNNING 任务立即暂停，写下你的改方向指令，
    恢复执行时 Agent 按你的指令继续（检查点可追溯，见执行轨迹）。"""
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    if t.status not in (TaskStatus.RUNNING, TaskStatus.PLANNING):
        raise HTTPException(409, f"只有执行中的任务可以接管，当前状态：{t.status.value}")
    note = (data.content or "老板接管：按最新要求继续").strip()
    await db.commit()
    # 1) 先抢占"接管权"：RUNNING/PLANNING → PAUSED 是 CAS，双击接管只有一次成功，
    #    另一次 409 —— 否则会写两份 human_takeover 检查点、重复暂停。
    try:
        await transition(db, t, TaskStatus.PAUSED, "user", user.id,
                         payload={"takeover": note[:200]})
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    # 2) 暂停所有 running executions（写 execution 检查点保留现场）
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "running"))
    execs = q.scalars().all()
    for e in execs:
        await pause_execution(db, e)
    # 3) 每个被暂停的执行写 human_takeover 检查点（恢复时取出指令注入）
    from app.models.task import ExecutionCheckpoint
    for e in execs:
        db.add(ExecutionCheckpoint(
            execution_id=e.id, checkpoint_type="human_takeover",
            step_index=3,
            context_snapshot={"note": note[:2000], "taken_by": user.id}))
    # 4) 干预留痕
    iv = Intervention(task_id=tid, user_id=user.id, type="TAKEOVER",
                     content=note, status="applied")
    db.add(iv)
    await db.commit()
    return {"detail": "已接管，任务暂停", "paused_executions": len(execs),
            "resume_via": f"POST /tasks/{tid}/takeover/resolve"}


@router.post("/{tid}/takeover/resolve", response_model=TaskActionOut)
async def resolve_takeover(tid: str, data: InterventionIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """V5.5 B1 接管解除：老板确认改完方向，任务从检查点恢复，
    最新接管指令作为最高优先级注入 Agent 上下文。"""
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
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
    await db.commit()
    # CAS：并发解除接管只有一个请求能把 PAUSED 翻回 RUNNING，也就只会提交一次续跑
    try:
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    # 与 /resume 同口径：只恢复最新 paused 执行（其余 superseded），队满回滚
    q = await db.execute(select(AgentExecution).where(
        AgentExecution.task_id == tid, AgentExecution.status == "paused")
        .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()))
    execs = list(q.scalars())
    if not execs:
        # 与 /resume 同口径：没有 paused 执行就按冷启动重新投递
        await _dispatch_or_revert(db, tid, t.assigned_agent_id, t.company_id,
                                  revert_to=TaskStatus.PAUSED)
        return {"detail": "接管已解除，任务重新开工"}
    for stale in execs[1:]:
        await db.execute(
            update(AgentExecution)
            .where(AgentExecution.id == stale.id,
                   AgentExecution.status == "paused")
            .values(status="superseded", completed_at=utcnow_iso())
            .execution_options(synchronize_session=False))
    await db.commit()
    _, submit_result = await resume_execution(db, execs[0])
    if submit_result == QUEUE_FULL:
        try:
            await transition(db, t, TaskStatus.PAUSED, "user", user.id)
            await db.commit()
        except IllegalTransition:
            await db.rollback()
        raise HTTPException(503, "后台执行队列已满，任务已回到暂停状态，请稍后重试")
    return {"detail": "接管已解除，任务从检查点恢复（含老板指令）"}


@router.post("/{tid}/retry", response_model=TaskStartedOut)
async def retry_task(tid: str,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """失败任务重试：FAILED→PLANNING→RUNNING，重新提交后台执行。

    前端任务详情页 FAILED 状态的"重试"按钮调用此端点。
    """
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    if t.status != TaskStatus.FAILED:
        raise HTTPException(409, f"只有 FAILED 状态的任务可以重试，当前状态：{t.status.value}")
    if not t.assigned_agent_id:
        raise HTTPException(400, "Task has no assigned agent")
    await db.commit()
    # 第一步 FAILED→PLANNING 就是抢占点：并发重试只有一个请求能成功，
    # 保证后台执行只被提交一次（重复提交=重复计费）。
    try:
        await transition(db, t, TaskStatus.PLANNING, "user", user.id)
        await transition(db, t, TaskStatus.RUNNING, "user", user.id)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    await _dispatch_or_revert(db, tid, t.assigned_agent_id, t.company_id)
    return {"detail": "retrying", "task_id": tid}


@router.post("/{tid}/cancel", response_model=TaskActionOut)
async def cancel_task(tid: str, reason: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    await db.commit()
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
    # 还没开始的排队条目一并取消；已在跑的不动（执行中的收尾沿用原行为）
    from app.services import job_queue
    await job_queue.cancel_active(
        db, kind=job_queue.TASK_EXEC_KIND,
        dedupe_key=job_queue.task_dedupe_key(tid), company_id=t.company_id)
    await db.commit()
    return {"detail": "cancelled"}


# ---------- Human In The Loop (P1-15) ----------
# class InterventionIn moved above its first use (takeover endpoints)


@router.post("/{tid}/interventions", status_code=201,
             response_model=TaskInterventionAddedOut)
async def add_intervention(tid: str, data: InterventionIn,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
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
        try:
            t.priority = TaskPriority(data.new_priority)
        except ValueError:
            raise HTTPException(400, "priority 必须是 LOW/NORMAL/HIGH/URGENT 之一")
        applied["priority"] = data.new_priority
    if data.new_agent_id and data.type == "AGENT_CHANGE":
        # 换负责人同样要校验 agent 归属，且必须与任务同公司
        agent = await assert_agent_owned(db, user.id, data.new_agent_id)
        if agent.company_id != t.company_id:
            raise HTTPException(404, "Agent not found")
        t.assigned_agent_id = data.new_agent_id
        applied["assigned_agent_id"] = data.new_agent_id
    await db.commit()
    await db.refresh(iv)
    return {"id": iv.id, "status": iv.status, "applied": applied}


@router.get("/{tid}/executions", response_model=list[TaskExecutionOut])
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


@router.get("/{tid}/logs", response_model=LogsOut)
async def task_logs(tid: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """任务日志：事件流 + 人工介入 + 执行记录摘要，按时间倒序。"""
    await _task_owned(db, user.id, tid)
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


@router.patch("/{tid}", response_model=TaskOut)
async def patch_task(tid: str, data: TaskPatchIn,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """更新任务可编辑字段（负责人/优先级/描述）。记录审计事件。"""
    t = await _task_owned(db, user.id, tid, Capability.OPERATE)
    changes = {}
    if "assigned_agent_id" in data.model_fields_set:
        if data.assigned_agent_id:
            # 换负责人需校验 agent 归属，且必须与任务同公司
            agent = await assert_agent_owned(db, user.id, data.assigned_agent_id)
            if agent.company_id != t.company_id:
                raise HTTPException(404, "Agent not found")
        t.assigned_agent_id = data.assigned_agent_id
        changes["assigned_agent_id"] = data.assigned_agent_id
    if data.priority is not None:
        # 枚举外取值会写坏数据库：自动派活的优先级 CASE 与前端排序都按枚举匹配，
        # 写成 "Urgent!" 这类值会让任务永远排在最后且无人察觉。
        valid = {p.value for p in TaskPriority}
        if data.priority not in valid:
            raise HTTPException(400, f"priority 必须是 {sorted(valid)} 之一")
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
