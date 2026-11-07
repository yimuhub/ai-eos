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
        # 修复：此前调用的 WorkflowRuntime.start 根本不存在（AttributeError），
        # 所有"启动工作流"调度动作 100% 失败。改走模块级 start_workflow，
        # 并与 automation_engine.run_workflow 一致：由后台任务继续推进执行。
        from app.workflow_runtime import start_workflow
        ex = await start_workflow(db, str(definition_id),
                                  started_by=f"scheduler:{sched.id}",
                                  input_data=params.get("input_data") or {})
        from app.background_jobs import submit_async

        _ex_id = str(ex.id)

        async def _advance(session):
            from app.workflow_runtime import advance_execution
            await advance_execution(session, _ex_id)

        submit_async(_advance)
        return {"workflow_execution_id": _ex_id}

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

    if sched.action_type == "ceo_exam_weekly":
        """CEO 周考：独立监督官出题评分，早于周度深度复盘。"""
        from app.exam_engine import run_exam

        rec = await run_exam(db, sched.company_id,
                             user_id=sched.created_by, source="schedule")
        return {"exam_id": rec.get("id"), "period": rec.get("period_key"),
                "status": rec.get("status"), "overall": rec.get("overall"),
                "reused": bool(rec.get("reused"))}

    if sched.action_type.startswith("ceo_evolve_"):
        """CEO 分级自我进化：日微调 / 周深度复盘 / 月迭代提案。"""
        from app.self_evolution import run_evolution

        level = sched.action_type.removeprefix("ceo_evolve_")
        rec = await run_evolution(db, sched.company_id, level,
                                  user_id=sched.created_by, source="schedule")
        return {"evolution_id": rec.get("id"), "level": level,
                "status": rec.get("status"),
                "reused": bool(rec.get("reused"))}

    if sched.action_type == "snapshot_performance":
        from app.performance_engine import snapshot_company
        results = await snapshot_company(db, sched.company_id)
        return {"agents": len(results),
                "top": results[0]["agent_name"] if results else None}

    if sched.action_type == "create_task_from_template":
        """从「任务模板」（automation_definitions kind=TASK_TEMPLATE）建任务。

        模板与调度必须同公司——模板 id 可能来自请求体，绝不能借调度
        把别家公司的模板内容复制成本公司的任务（跨租户泄露 + 伪造来源）。
        指派员工不属于本公司时**清空指派**而不是让整条调度失败：
        模板可能来自公司重组前，留存任务比丢掉这一次执行更有价值。
        """
        from app.models.agent import Agent
        from app.models.automation import AutomationDefinition
        from app.models.company import Company
        from app.models.task import (Task, TaskEventType, TaskPriority,
                                     TaskStatus)
        from app.services.event_system import log_event
        from app.task_engine import add_task_event

        template_id = params.get("template_id")
        if not template_id:
            raise ValueError("template_id required")

        defn = await db.get(AutomationDefinition, template_id)
        if (defn is None
                or (defn.trigger_config or {}).get("kind") != "TASK_TEMPLATE"
                or defn.company_id != sched.company_id):
            # 三种情况统一报同一句：不向调用方区分「不存在」与「别家的」
            raise ValueError(f"task template not found: {template_id}")

        step = next((s for s in (defn.steps or [])
                     if isinstance(s, dict) and s.get("type") == "create_task"),
                    None)
        if step is None:
            raise ValueError(f"template has no create_task step: {template_id}")
        tparams = step.get("params") or {}

        title = (tparams.get("title") or defn.name or "任务模板").strip() or "任务模板"
        raw_priority = tparams.get("priority") or TaskPriority.NORMAL.value
        try:
            priority = TaskPriority(str(raw_priority).upper())
        except ValueError:
            # 模板是用户可编辑的 JSON：脏优先级只降级，不让调度整轮失败
            log.warning("模板 %s 的 priority=%r 非法，按 NORMAL 处理",
                        template_id, raw_priority)
            priority = TaskPriority.NORMAL

        assigned_agent_id = tparams.get("assigned_agent_id") or None
        if assigned_agent_id:
            agent = await db.get(Agent, assigned_agent_id)
            if agent is None or agent.company_id != sched.company_id:
                log.warning("模板 %s 指派的员工 %s 不属于公司 %s，已清空",
                            template_id, assigned_agent_id, sched.company_id)
                assigned_agent_id = None

        # tasks.created_by 是非空外键：无创建人的调度回退到公司 owner，
        # 保证历史/脚本创建的调度也能按时产出任务。
        created_by = sched.created_by
        if not created_by:
            company = await db.get(Company, sched.company_id)
            created_by = company.owner_id if company else None
        if not created_by:
            raise ValueError(f"cannot resolve created_by for schedule {sched.id}")

        task = Task(
            company_id=sched.company_id, title=title[:500],
            status=TaskStatus.TODO, priority=priority,
            created_by=created_by)
        if "description" in tparams and tparams["description"] is not None:
            task.description = tparams["description"]
        if tparams.get("project_id"):
            task.project_id = tparams["project_id"]
        if assigned_agent_id:
            task.assigned_agent_id = assigned_agent_id
        db.add(task)
        await db.flush()

        await add_task_event(db, task.id, TaskEventType.CREATED, "system",
                             sched.created_by, {"title": task.title,
                                                "template_id": template_id})
        await log_event(db, "task.created_from_template", "task", task.id,
                        "system", sched.created_by,
                        {"title": task.title, "template_id": template_id,
                         "schedule_id": sched.id},
                        company_id=sched.company_id)
        await db.commit()
        return {"task_id": task.id, "template_id": template_id}

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

    if sched.action_type == "ceo_digest":
        # 超级 Agent 日报：统计近 24h 统筹/任务/成本/卡点，站内信 + 推送给老板
        from app.ceo_engine import push_digest
        digest = await push_digest(db, sched.company_id)
        return {"generated_at": digest["generated_at"],
                "tokens": digest["stats"].get("tokens", 0),
                "stuck_tasks": digest["stats"].get("stuck_tasks", 0)}

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
    # 只读取模块级 _loop_task，无需 global 声明（pyflakes 会报未使用）
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
