"""Scheduler engine (P2-15): dispatch due schedules + background loop."""
import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.scheduler import AutomationRun, AutomationSchedule, compute_next_run

log = get_logger(__name__)

# 抢占租约：抢到后 120 秒内其他扫描者不会重复执行同一条。
# 若执行超过租约（例如 LLM 动作很慢），到期后仍可能被再次抢占——
# 这是"至少一次"语义的代价；AutomationRun 历史可用于对账。
CLAIM_LEASE_SECONDS = 120


def _now():
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def execute_action(db: AsyncSession, sched: AutomationSchedule) -> dict:
    """根据 action_type 执行对应动作。"""
    params = sched.action_params or {}

    if sched.action_type == "start_workflow":
        definition_id = params.get("definition_id")
        if not definition_id:
            raise ValueError("definition_id required")
        from app.workflow_runtime import WorkflowRuntime
        rt = await WorkflowRuntime.start(db, definition_id,
                                         input_data=params.get("input_data", {}),
                                         triggered_by=f"scheduler:{sched.id}")
        return {"workflow_execution_id": str(rt.execution.id)}

    if sched.action_type == "send_message":
        from app.collaboration.routing import send_collaboration_message
        msg = await send_collaboration_message(
            db=db, sender_id=params["sender_agent_id"],
            message_type=params.get("message_type", "PROGRESS"),
            title=params.get("title", sched.name),
            content=params.get("content", ""),
            company_id=sched.company_id)
        return {"message_id": msg.id}

    if sched.action_type == "reflect_agent":
        """Agent 自我反思：归纳近期经历为长期记忆（Hermes式整理）。"""
        from app.models.agent import Agent
        from app.reflection_engine import self_reflect

        agent_id = params.get("agent_id")
        agent = await db.get(Agent, agent_id) if agent_id else None
        if not agent:
            raise ValueError("agent_id required / not found")
        r = await self_reflect(db, agent)
        return {"reflected": r["reflected"],
                "memories_written": r.get("memories_written", 0)}

    if sched.action_type == "snapshot_performance":
        from app.performance_engine import snapshot_company
        results = await snapshot_company(db, sched.company_id)
        return {"agents": len(results),
                "top": results[0]["agent_name"] if results else None}

    if sched.action_type == "daily_briefing":
        # V5.5 A1：CEO 简报推送——生成简报全文写进事件流，
        # 老板打开 App 首页/事件流即可看到"总经理的早间汇报"
        from app.models.company import Company
        from app.models.event import EventLog
        from app.api.routes.briefing import build_briefing
        company = await db.get(Company, sched.company_id)
        if not company:
            raise ValueError("company not found")
        brief = await build_briefing(db, company)
        ev = EventLog(
            event_type="BRIEFING_PUSH",
            entity_type="company", entity_id=sched.company_id,
            actor_type="system",
            payload={"name": sched.name, "briefing": brief})
        db.add(ev)
        await db.commit()
        return {"event_id": ev.id,
                "greeting": brief.get("greeting", ""),
                "needs_you": brief.get("needs_you", {}).get("count", 0)}

    raise ValueError(f"unknown action_type {sched.action_type}")


async def _claim_schedule(db: AsyncSession, sid: str, now: datetime) -> bool:
    """条件 UPDATE 原子抢占一条到期调度。

    兼容现有表结构（不新增列）：把 ``next_run_at`` 推到 ``now + 120s``
    作为租约令牌，效果等价于 ``locked_until = now + 120s``——其他扫描者
    读到 ``next_run_at > now`` 就不会再执行同一条。

    必须在执行动作前 commit：抢占是跨连接可见的，只有提交后其它 worker
    才会看到"已被占用"。``rowcount == 1`` 才说明本次抢到。
    """
    res = await db.execute(
        update(AutomationSchedule)
        .where(
            AutomationSchedule.id == sid,
            AutomationSchedule.status == "ACTIVE",
            AutomationSchedule.next_run_at.is_not(None),
            AutomationSchedule.next_run_at <= now,
        )
        .values(next_run_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS)))
    await db.commit()
    return res.rowcount == 1


async def run_due_schedules(db: AsyncSession,
                            company_id: str | None = None) -> int:
    """执行所有到期的调度（由后台循环 / tick 端点调用）。

    company_id 不为空时只扫描该公司的调度（tick 端点用，避免普通用户
    手动触发全平台所有公司的任务）。

    返回本次**抢占到**的调度数量；并发调用时同一行只会被一个调用方抢到。
    """
    now = _now()
    q = select(AutomationSchedule.id).where(
        AutomationSchedule.status == "ACTIVE",
        AutomationSchedule.next_run_at.is_not(None),
        AutomationSchedule.next_run_at <= now)
    if company_id:
        q = q.where(AutomationSchedule.company_id == company_id)
    due_ids = (await db.execute(q)).scalars().all()
    # 结束只读事务再抢占：SQLite/WAL 下"读快照 → 升级为写"在其它连接
    # 提交后会直接得到 SQLITE_BUSY_SNAPSHOT（busy_timeout 也救不了）。
    # 先 commit 关闭快照，让每条 UPDATE 从全新事务开始等待写锁。
    await db.commit()

    claimed = 0
    for sid in due_ids:
        if not await _claim_schedule(db, sid, now):
            continue  # 已被其他扫描者抢走（或期间被暂停/改期）
        claimed += 1
        sched = await db.get(AutomationSchedule, sid)
        if sched is None:  # 抢占后被删除：claim 的租约随行消失
            continue

        # 动作执行期间可能 commit（如 daily_briefing），先把需要用的字段
        # 拷出来，避免 ORM 过期后访问触发隐式 IO。
        trigger_type = sched.trigger_type
        trigger_config = sched.trigger_config
        run_count = sched.run_count or 0
        run = AutomationRun(id=str(uuid.uuid4()), schedule_id=sid)
        db.add(run)
        started_at = run.started_at
        try:
            result = await execute_action(db, sched)
            run.status = "SUCCESS"
            run.result = result
        except Exception as e:
            log.exception("调度 %s（%s）执行失败", sid, trigger_type)
            run.status = "FAILED"
            run.error = f"{type(e).__name__}: {e}"
            # 动作里可能留下半成品写入/失败的事务：回滚后再写运行记录，
            # 否则一条失败动作会让整轮扫描（及后续调度）全部中止。
            try:
                await db.rollback()
                db.add(run)
            except Exception:
                log.exception("调度 %s 失败后回滚/重挂运行记录失败", sid)
        finally:
            run.finished_at = _now()
            sched.last_run_at = started_at
            sched.run_count = run_count + 1
            # 执行完再写真正的下次运行时间，覆盖抢占时写入的租约。
            sched.next_run_at = compute_next_run(trigger_type,
                                                 trigger_config, now)
            try:
                await db.commit()
            except Exception:
                # 落库失败不要炸掉整轮循环：租约 120s 后自动过期可重试
                log.exception("调度 %s 运行结果落库失败", sid)
                await db.rollback()
    return claimed


_loop_task: asyncio.Task | None = None


async def scheduler_loop():
    """后台调度循环：每 10 秒扫一次到期调度，并拉起排队中的自动任务。"""
    from app.db.session import AsyncSessionLocal
    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await run_due_schedules(db)
                if n:
                    log.info("[scheduler] executed %d schedule(s)", n)
        except Exception:
            log.exception("[scheduler] loop error")
        # 自动任务队列：批量派活时 worker 满员，剩余任务在这里被逐步拉起。
        # 与到期调度分开 try：调度失败不能连带停掉任务队列。
        try:
            from app.task_runner import dispatch_auto_queue
            async with AsyncSessionLocal() as db:
                started = await dispatch_auto_queue(db)
                if started:
                    log.info("[scheduler] auto-started %d queued task(s)", started)
        except Exception:
            log.exception("[scheduler] auto dispatch error")
        await asyncio.sleep(10)


def start_scheduler():
    """在应用启动时调用（lifespan）。"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(scheduler_loop())
        log.info("[scheduler] background loop started")


def stop_scheduler():
    global _loop_task
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
