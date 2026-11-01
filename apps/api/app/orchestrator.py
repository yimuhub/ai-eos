"""Multi-Agent Collaboration Coordinator.

Parent task -> CEO/Coordinator LLM decomposes -> subtasks created & persisted
-> agents run in parallel -> results collected -> coordinator aggregates.
"""
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import run_agent_on_task
from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.task import Task, TaskPriority, TaskStatus
from app.services.event_system import log_event
from app.task_engine import transition

logger = get_logger(__name__)

DECOMPOSE_PROMPT = """你是 AI 公司的 CEO（协调者）。

主任务：{title}
详情：{description}

可用员工：
{roster}

请把主任务拆解为 2-4 个可并行执行的子任务，分配给最合适的员工。
严格返回如下 JSON（不要其他文字）：
{{"subtasks": [{{"title": "...", "description": "...", "agent_name": "员工名"}}]}}
"""

SUMMARIZE_PROMPT = """你是 AI 公司的 CEO。以下是各员工完成子任务的成果：

{results}

请汇总为一份完整的最终报告（Markdown 格式），包含：概述、各部分要点、结论与建议。
"""


async def _chat(db: AsyncSession, agent: Agent, messages: list[dict]) -> dict:
    from app.agent_runtime import build_context
    from app.model_gateway import ModelGateway
    _, model_ids = await build_context(db, agent, Task(
        id="tmp", company_id=agent.company_id, title="", description="",
        status=TaskStatus.TODO, priority=TaskPriority.NORMAL, created_by="system"))
    gateway = ModelGateway(db)
    return await gateway.chat_with_agent_models(model_ids, messages)


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("No JSON found")


async def orchestrate(db: AsyncSession, parent: Task, coordinator: Agent) -> Task:
    """Full coordination cycle. Caller commits."""
    roster = (await db.execute(
        select(Agent).where(Agent.company_id == parent.company_id))).scalars().all()
    roster_lines = "\n".join(f"- {a.name}（岗位: {a.role or '未设置'}）" for a in roster)
    name_to_agent = {a.name: a for a in roster}

    # 1. Decompose
    llm = await _chat(db, coordinator, [
        {"role": "system", "content": coordinator.system_prompt or "你是一位高效的CEO。"},
        {"role": "user", "content": DECOMPOSE_PROMPT.format(
            title=parent.title, description=parent.description or "", roster=roster_lines)},
    ])
    plan = _extract_json(llm["content"])

    subtasks: list[tuple[Task, Agent]] = []
    for st in plan.get("subtasks", [])[:5]:
        agent = name_to_agent.get(st.get("agent_name", ""))
        if not agent:
            continue
        sub = Task(company_id=parent.company_id, parent_task_id=parent.id,
                   title=st["title"], description=st.get("description"),
                   status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                   assigned_agent_id=agent.id, created_by=parent.created_by)
        db.add(sub)
        await db.flush()
        subtasks.append((sub, agent))

    await log_event(db, "orchestrator.decomposed", "task", parent.id, "agent",
                    coordinator.id,
                    {"subtask_count": len(subtasks),
                     "titles": [s[0].title for s in subtasks]})

    if not subtasks:
        raise RuntimeError("Coordinator produced no valid subtasks")

    await db.commit()

    # 2. Execute subtasks in parallel (own sessions per worker);
    #    each worker returns its result text so no cross-session object reads.
    async def run_one(task_id: str, agent_id: str, agent_name: str, title: str) -> dict:
        from app.background_jobs import _make_session_factory, dispose_factory_engine
        factory = _make_session_factory()
        try:
            async with factory() as session:
                t = await session.get(Task, task_id)
                a = await session.get(Agent, agent_id)
                await run_agent_on_task(session, a, t, actor_type="agent")
                await transition(session, t, TaskStatus.COMPLETED, "agent", agent_id)
                return {"task": title, "agent": agent_name,
                        "status": "COMPLETED", "output": t.result or ""}
        except Exception as exc:
            try:
                async with factory() as s2:
                    t2 = await s2.get(Task, task_id)
                    failed_now = False
                    if t2 and t2.status == TaskStatus.RUNNING:
                        t2.status = TaskStatus.FAILED
                        failed_now = True
                    if failed_now:
                        from app.services.notification_service import notify_task_terminal
                        await notify_task_terminal(
                            s2, t2, "FAILED", error=str(exc)[:500])
                    await log_event(s2, "orchestrator.subtask_failed", "task",
                                    task_id, "agent", agent_id, {"error": str(exc)[:500]})
                    await s2.commit()
            except Exception as mark_err:
                # 失败标记本身也失败：不能再吞——否则子任务会永久停在 RUNNING
                logger.error("子任务失败标记落库失败 task=%s agent=%s 原始错误=%s: %s",
                             task_id, agent_id, exc, mark_err, exc_info=True)
            return {"task": title, "agent": agent_name, "status": "FAILED",
                    "output": "", "error": str(exc)[:500]}
        finally:
            # 每个子任务一个独立引擎：用完即 dispose，避免连接池泄漏
            await dispose_factory_engine(factory)

    outcomes = await asyncio.gather(
        *[run_one(t.id, a.id, a.name, t.title) for t, a in subtasks],
        return_exceptions=True)

    # 3. Collect from worker-returned plain dicts (no ORM cache issues)
    collected = []
    failed = []
    for out in outcomes:
        if isinstance(out, Exception):
            # 之前这里只记 "(unknown)"，把异常本身丢掉了——失败原因不可追溯
            logger.error("子任务执行抛出未捕获异常: %s", out, exc_info=out)
            failed.append({"task": "(未捕获异常)", "agent": "",
                           "status": f"EXCEPTION: {type(out).__name__}: {str(out)[:200]}"})
            continue
        if out["status"] == "COMPLETED" and out["output"]:
            collected.append(f"## {out['task']}\n（执行者: {out['agent']}）\n\n{out['output']}\n")
        else:
            failed.append({"task": out["task"], "agent": out["agent"],
                           "status": out["status"]})

    # 4. Aggregate final report
    final_llm = await _chat(db, coordinator, [
        {"role": "system", "content": coordinator.system_prompt or "你是一位高效的CEO。"},
        {"role": "user", "content": SUMMARIZE_PROMPT.format(results="\n".join(collected))},
    ])
    report = final_llm["content"]
    if failed:
        report += f"\n\n---\n⚠ 失败子任务（需人工关注）: "
        report += ", ".join(f"{f['task']}（{f['agent']}，状态 {f['status']}）" for f in failed)

    parent.result = report
    # 产出即归档：CEO 汇总报告自动存入资产中心（与单任务完成自动归档一致）
    try:
        from app.api.routes.assets import save_artifact
        await save_artifact(db, parent.company_id,
                            f"CEO汇总报告-{(parent.title or 'orchestration')[:40]}.md",
                            report, task_id=parent.id, agent_id=coordinator.id)
        await db.flush()
    except Exception as e:
        # 归档失败不影响编排结果本身，但必须留痕
        logger.warning("编排报告自动归档失败 task=%s: %s", parent.id, e, exc_info=True)
    await db.commit()
    await db.refresh(parent)
    return parent
