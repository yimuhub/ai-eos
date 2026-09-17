"""Advanced Automation engine (P3-12)."""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import (AutomationDefinition, AutomationRunRecord,
                                   MAX_DEPTH, MAX_STEPS_PER_RUN, new_id)


async def _execute_step(db: AsyncSession, company_id: str, step: dict,
                        mode: str, run_id: str, depth: int) -> dict:
    """执行单步。TEST 模式只模拟，不产生副作用。"""
    stype = step.get("type")
    params = step.get("params", {})

    if mode == "TEST":
        return {"step_type": stype, "simulated": True, "params": params}

    if stype == "send_message":
        from app.collaboration.routing import send_collaboration_message
        msg = await send_collaboration_message(
            db=db, sender_id=params["sender_agent_id"],
            message_type=params.get("message_type", "PROGRESS"),
            title=params.get("title", "自动化消息"),
            content=params.get("content", ""), company_id=company_id)
        return {"message_id": msg.id}

    if stype == "start_meeting":
        from app.api.routes.meetings import _kick  # noqa
        from app.models.meeting import Meeting, MeetingParticipant, MeetingStatus, MeetingType  # noqa
        meeting = Meeting(
            id=str(__import__("uuid").uuid4()), company_id=company_id,
            title=params.get("title", "自动化会议"),
            topic=params.get("topic"), project_id=params.get("project_id"),
            meeting_type=params.get("meeting_type", "STRATEGY_MEETING"),
            status=MeetingStatus.RUNNING)
        db.add(meeting)
        await db.flush()
        for aid in params.get("participant_ids", []):
            db.add(MeetingParticipant(meeting_id=meeting.id, agent_id=aid))
        await db.commit()
        from app.meeting_engine import schedule_next_round
        schedule_next_round(str(meeting.id))
        return {"meeting_id": meeting.id}

    if stype == "create_task":
        from app.models.task import Task, TaskStatus
        from app.models.company import Company
        # tasks.created_by 是 users FK；自动化创建的任务记为公司 owner
        comp = await db.get(Company, company_id)
        task = Task(company_id=company_id, title=params.get("title", "自动化任务"),
                    description=params.get("description"),
                    status=TaskStatus.TODO,
                    created_by=comp.owner_id if comp else None)
        if params.get("project_id"):
            task.project_id = params["project_id"]
        if params.get("assigned_agent_id"):
            task.assigned_agent_id = params["assigned_agent_id"]
        db.add(task)
        await db.flush()
        return {"task_id": task.id}

    raise ValueError(f"未知步骤类型: {stype}")


async def run_automation(db: AsyncSession, definition: AutomationDefinition,
                         trigger: str = "MANUAL", depth: int = 0,
                         resume_run: AutomationRunRecord | None = None) -> AutomationRunRecord:
    """执行一次多步自动化。含深度限制、步数限制、循环检测。
    传入 resume_run 时从该 run 的断点（WAITING_APPROVAL 处）继续。"""
    if depth >= MAX_DEPTH:
        run = AutomationRunRecord(id=new_id(), definition_id=definition.id,
                                  mode=definition.mode, status="FAILED",
                                  depth=depth, error=f"超过子流程嵌套上限 {MAX_DEPTH}")
        db.add(run)
        await db.commit()
        return run

    steps = definition.steps or []
    if len(steps) > MAX_STEPS_PER_RUN:
        steps = steps[:MAX_STEPS_PER_RUN]

    if resume_run:
        # 断点恢复：沿用已有 run，跳过已完成/已批准的步骤
        run = resume_run
        done_keys = {s.get("seq") for s in (run.step_results or [])
                     if s.get("status") in ("SUCCESS", "APPROVED")}
        start_idx = max(done_keys) if done_keys else 0
        step_results = [s for s in (run.step_results or [])
                        if s.get("status") != "APPROVED"] or list(run.step_results or [])
    else:
        run = AutomationRunRecord(id=new_id(), definition_id=definition.id,
                                  mode=definition.mode, depth=depth)
        db.add(run)
        await db.flush()
        start_idx = 0
        step_results = []

    status = "SUCCESS"
    error = None
    seen_steps = set()

    try:
        for i, step in enumerate(steps):
            if i < start_idx:
                continue  # 断点前已完成的步骤直接跳过
            # 循环检测：同 type+params 连续重复 3 次以上视为循环
            key = json.dumps({k: step.get(k) for k in ("type", "params")},
                             sort_keys=True, default=str)
            seen_steps.add(key)
            if len(seen_steps) < i // 3 + 1:
                raise RuntimeError("检测到循环执行")

            # 条件判断（简单版）：condition 存在时检查前一步结果
            cond = step.get("condition")
            last_ok = (step_results[-1]["status"] == "SUCCESS"
                       if step_results else True)
            if cond and not last_ok:
                step_results.append({"seq": i + 1, "type": step.get("type"),
                                     "status": "SKIPPED", "reason": "条件不满足"})
                continue

            # 审批步骤
            if step.get("type") == "wait_approval" or step.get("requires_approval"):
                step_results.append({"seq": i + 1, "type": step.get("type"),
                                     "status": "WAITING_APPROVAL"})
                status = "WAITING_APPROVAL"
                break

            try:
                if definition.mode == "TEST":
                    result = {"step_type": step.get("type"),
                              "simulated": True,
                              "params": step.get("params", {})}
                else:
                    result = await _execute_step(db, definition.company_id,
                                                 step, definition.mode,
                                                 run.id, depth)
                step_results.append({"seq": i + 1,
                                     "type": step.get("type"),
                                     "status": "SUCCESS", "result": result})
            except Exception as e:
                on_failure = step.get("on_failure", "abort")
                step_results.append({"seq": i + 1, "type": step.get("type"),
                                     "status": "FAILED",
                                     "error": str(e)[:200]})
                if on_failure == "continue":
                    continue
                status = "FAILED"
                error = f"步骤{i+1}失败: {e}"
                break
    except Exception as e:
        status = "FAILED"
        error = str(e)[:300]

    run.status = status
    run.step_results = step_results
    run.error = error
    run.finished_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).replace(tzinfo=None)
    await db.commit()
    return run


async def run_sub_automation(db: AsyncSession, parent_company: str,
                             sub_definition_id: str, depth: int):
    """子流程入口（带深度计数）。"""
    sub = await db.get(AutomationDefinition, sub_definition_id)
    if not sub:
        return None
    return await run_automation(db, sub, trigger="SUB_AUTOMATION",
                                depth=depth + 1)
