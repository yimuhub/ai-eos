"""Multi-Agent Collaboration Coordinator.

Parent task -> CEO/Coordinator LLM decomposes -> subtasks created & persisted
-> agents run in parallel -> results collected -> coordinator aggregates.
"""
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime import run_agent_on_task
from app.models.agent import Agent
from app.models.task import Task, TaskPriority, TaskStatus
from app.services.event_system import log_event
from app.task_engine import transition

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
        from app.background_jobs import _make_session_factory
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
                    if t2 and t2.status == TaskStatus.RUNNING:
                        t2.status = TaskStatus.FAILED
                    await log_event(s2, "orchestrator.subtask_failed", "task",
                                    task_id, "agent", agent_id, {"error": str(exc)[:500]})
                    await s2.commit()
            except Exception:
                pass
            return {"task": title, "agent": agent_name, "status": "FAILED",
                    "output": "", "error": str(exc)[:500]}

    outcomes = await asyncio.gather(
        *[run_one(t.id, a.id, a.name, t.title) for t, a in subtasks],
        return_exceptions=True)

    # 3. Collect from worker-returned plain dicts (no ORM cache issues)
    collected = []
    failed = []
    for out in outcomes:
        if isinstance(out, Exception):
            failed.append({"task": "(unknown)", "agent": "", "status": "EXCEPTION"})
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
    await db.commit()
    await db.refresh(parent)
    return parent
