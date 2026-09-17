"""Scheduler engine (P2-15): dispatch due schedules + background loop."""
import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler import AutomationRun, AutomationSchedule, compute_next_run


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

    raise ValueError(f"unknown action_type {sched.action_type}")


async def run_due_schedules(db: AsyncSession) -> int:
    """执行所有到期的调度（由后台循环周期调用）。"""
    now = _now()
    q = await db.execute(select(AutomationSchedule).where(
        AutomationSchedule.status == "ACTIVE",
        AutomationSchedule.next_run_at.is_not(None),
        AutomationSchedule.next_run_at <= now))
    due = q.scalars().all()
    count = 0
    for sched in due:
        run = AutomationRun(id=str(uuid.uuid4()), schedule_id=sched.id)
        db.add(run)
        try:
            result = await execute_action(db, sched)
            run.status = "SUCCESS"
            run.result = result
        except Exception as e:
            run.status = "FAILED"
            run.error = f"{type(e).__name__}: {e}"
        finally:
            run.finished_at = _now()
            sched.last_run_at = run.started_at
            sched.run_count += 1
            sched.next_run_at = compute_next_run(sched.trigger_type,
                                                 sched.trigger_config, now)
            await db.commit()
            count += 1
    return count


_loop_task: asyncio.Task | None = None


async def scheduler_loop():
    """后台调度循环：每 10 秒扫一次到期调度。"""
    from app.db.session import AsyncSessionLocal
    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await run_due_schedules(db)
                if n:
                    print(f"[scheduler] executed {n} schedule(s)")
        except Exception as e:
            print(f"[scheduler] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(10)


def start_scheduler():
    """在应用启动时调用（lifespan）。"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(scheduler_loop())
        print("[scheduler] background loop started")


def stop_scheduler():
    global _loop_task
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
