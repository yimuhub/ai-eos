"""经验沉淀与跨 Agent 学习：失败→教训、成功→可复用工作流。

## 触发时机

Agent 每跑完一条任务，运行时都会调用 `harvest_task_experience`：

* 一次就成功：只在同类任务还没有任何经验时，存一条基准成功路径；
* 失败：至少两次尝试才值得沉淀，单次抖动不是教训；
* 先失败后成功：升级为成功经验，同时保留踩坑记录。

## 复用方式

`build_experience_context` 在任务开工前注入两段内容：
1. **已验证工作流**——同类任务已经跑通的步骤，要求按序执行而不是重新试错；
2. **团队经验**——本 Agent 与同事的成功路径、失败教训、成本提示。

`adopt_experience` 允许员工主动把同事经验写进自己的长期记忆。

## 成本纪律

经验提炼只做一次小模型调用（max_tokens 约 900），模型不可用时退回确定性模板；
经验与工作流的复用本身不再额外调用模型，避免"为了省 Token 反而烧 Token"。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.experience import SharedExperience
from app.models.task import AgentExecution, Task, TaskStatus
from app.models.token_usage import TokenUsage
from app.models.workflow import (NodeType, WorkflowDefinition, WorkflowEdge,
                                 WorkflowNode, WorkflowStatus)
from app.workflow_authoring import latest_workflow, reuse_key_for

logger = get_logger(__name__)

PUBLISH_MIN_ATTEMPTS = 2
CONTEXT_CHAR_BUDGET = 4200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _env_on(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _str_list(value, limit: int = 8, width: int = 300) -> list[str]:
    out: list[str] = []
    for item in (value or []):
        text = str(item or "").strip()[:width]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def experience_brief(row: SharedExperience) -> dict:
    return {
        "id": row.id, "company_id": row.company_id,
        "agent_id": row.agent_id, "author_agent_name": row.author_agent_name,
        "task_id": row.task_id, "task_title": row.task_title,
        "reuse_key": row.reuse_key, "kind": row.kind, "scope": row.scope,
        "status": row.status, "summary": row.summary,
        "steps": row.steps or [], "pitfalls": row.pitfalls or [],
        "lessons": row.lessons or [], "token_tips": row.token_tips or [],
        "quality_tips": row.quality_tips or [], "tags": row.tags or [],
        "attempts": int(row.attempts or 1),
        "success_count": int(row.success_count or 0),
        "failure_count": int(row.failure_count or 0),
        "tokens_used": int(row.tokens_used or 0),
        "tokens_saved_est": int(row.tokens_saved_est or 0),
        "confidence": round(float(row.confidence or 0.0), 2),
        "workflow_id": row.workflow_id,
        "workflow_version": row.workflow_version,
        "adopted_by": row.adopted_by or [],
        "source": row.source, "meta": row.meta or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _task_tokens(db: AsyncSession, task_id: str) -> int:
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .where(TokenUsage.task_id == task_id))).first()
    return int(row[0] or 0)


def _steps_from_output(output: str) -> list[str]:
    import re

    steps: list[str] = []
    for line in (output or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#") or re.match(r"^(\d+[.、)）]|[-*])\s+", text):
            clean = re.sub(r"^(#+|\d+[.、)）]|[-*])\s*", "", text).strip()[:180]
            if clean and clean not in steps:
                steps.append(clean)
        if len(steps) >= 5:
            break
    if not steps and output.strip():
        for para in output.strip().split("\n\n")[:3]:
            clean = para.strip().replace("\n", " ")[:180]
            if clean:
                steps.append(clean)
    return steps[:5]


def _fallback_narrative(task: Task, executions: list[AgentExecution],
                        success: bool, output: str,
                        error: str | None) -> dict:
    failures = [e for e in executions if (e.status == "failed" or e.error)]
    errors = _str_list([e.error for e in failures], limit=4, width=240)
    if success:
        steps = _steps_from_output(output) or [
            "对齐目标与验收标准，先确认交付物长什么样",
            "复现上次成功路径，不重新试错",
            "交付前逐条对照验收标准自检",
        ]
        return {
            "summary": (f"《{task.title}》经过 {len(executions)} 次尝试后成功；"
                        "失败尝试的原因已记录，下次直接复用成功路径。"),
            "steps": steps,
            "pitfalls": errors or ["失败尝试缺少可追溯的错误记录"],
            "lessons": ["先归因再重试：同类失败出现两次就换方案，而不是继续原路重跑"],
            "token_tips": ["开工先查已验证工作流与经验，避免重复试错消耗 Token"],
            "quality_tips": ["交付前按验收标准逐条复核，并保留可复算的证据"],
            "tags": [reuse_key_for(task.title), "成功路径"],
        }
    return {
        "summary": (f"《{task.title}》在 {len(executions)} 次尝试中失败；"
                    f"最近一次错误：{(error or (errors[0] if errors else '未知'))[:160]}"),
        "steps": [
            "开工前补齐导致失败的前置条件（输入、权限、数据、模型配置）",
            "把任务拆成可单独验证的小步，每步先验收再继续",
            "同类失败出现两次立即停下归因，必要时换人/换流程",
        ],
        "pitfalls": errors or [(error or "失败原因未记录")[:240]],
        "lessons": ["连续失败不是努力问题，是方案问题；先归因再决定重试还是换路"],
        "token_tips": ["失败重跑前先检查输入与工具，避免把同一错误再烧一遍 Token"],
        "quality_tips": ["失败也要留下可检索的错误证据，供下一位同事避坑"],
        "tags": [reuse_key_for(task.title), "失败教训"],
    }


async def _llm_narrative(db: AsyncSession, agent, company_id: str,
                         task: Task, executions: list[AgentExecution],
                         success: bool, output: str,
                         error: str | None) -> tuple[dict | None, dict]:
    if not _env_on("AIEOS_EXPERIENCE_LLM", True):
        return None, {"tokens": 0, "cost": 0.0}
    from app.model_gateway import ModelGateway, usage_context

    failures = [e for e in executions if (e.status == "failed" or e.error)]
    prompt = (
        f"任务：{task.title}\n描述：{(task.description or '')[:1200]}\n"
        f"结果：{'成功' if success else '失败'}；"
        f"尝试 {len(executions)} 次，失败 {len(failures)} 次。\n"
        f"失败记录：{_str_list([e.error for e in failures], 4, 300)}\n"
        f"{'成功产出' if success else '最近错误'}："
        f"{(output if success else (error or ''))[:1800]}\n\n"
        "请提炼可复用于同类任务的经验。只输出 JSON："
        "{\"summary\":\"一句话\",\"steps\":[\"可执行步骤\"],"
        "\"pitfalls\":[\"踩坑/错误信号\"],\"lessons\":[\"教训\"],"
        "\"token_tips\":[\"省 Token 的具体做法\"],"
        "\"quality_tips\":[\"提升质量的具体做法\"],\"tags\":[\"检索标签\"]}"
    )
    started = _utcnow()
    try:
        with usage_context(company_id=company_id, agent=str(agent.id),
                           scenario="LEARNING"):
            res = await ModelGateway(db).chat_with_agent_models_by_agent(
                agent, [{"role": "user", "content": prompt}],
                config={"max_tokens": 1000})
    except Exception as exc:  # noqa: BLE001 — 提炼失败退确定性模板
        logger.warning("经验提炼跳过 company=%s agent=%s: %s: %s",
                       company_id, agent.id, type(exc).__name__, exc)
        return None, {"tokens": 0, "cost": 0.0}
    from app.self_evolution import _extract_json
    usage = res.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.agent_id == str(agent.id),
               TokenUsage.scenario == "LEARNING",
               TokenUsage.created_at >= started))).first()
    return _extract_json(res.get("content") or ""), {
        "tokens": max(tokens, int(row[0] or 0)),
        "cost": round(float(row[1] or 0.0), 4)}


def _normalize_narrative(raw: dict | None, fallback: dict) -> dict:
    out = dict(fallback)
    if not isinstance(raw, dict):
        return out
    summary = str(raw.get("summary") or "").strip()[:600]
    if summary:
        out["summary"] = summary
    for src, dst in (("steps", "steps"), ("pitfalls", "pitfalls"),
                     ("lessons", "lessons"), ("token_tips", "token_tips"),
                     ("quality_tips", "quality_tips"), ("tags", "tags")):
        values = _str_list(raw.get(src), limit=8)
        if values:
            out[dst] = values
    return out


async def _ensure_workflow(db: AsyncSession, task: Task, agent, *,
                           reuse_key: str, steps: list[str],
                           experience_id: str, success: bool,
                           attempts: int) -> WorkflowDefinition | None:
    """成功经验至少有一条可执行工作流；失败只关联已有流程，不新建。"""
    prior = await latest_workflow(db, str(task.company_id), reuse_key)
    if not success:
        return prior
    if prior is not None:
        if prior.source == "experience":
            meta = dict(prior.meta or {})
            task_ids = list(meta.get("task_ids") or [])
            if str(task.id) not in task_ids:
                task_ids.append(str(task.id))
                prior.run_count = int(prior.run_count or 0) + 1
                prior.success_count = int(prior.success_count or 0) + 1
                meta["task_ids"] = task_ids[-20:]
                prior.meta = meta
            return prior
        # 已有 CEO 生成的流程：直接复用，不覆盖、不归档
        return prior

    wf = WorkflowDefinition(
        id=str(uuid.uuid4()), company_id=str(task.company_id),
        project_id=task.project_id, name=f"{task.title} · 经验工作流"[:300],
        description=(f"由 {agent.name} 在 {attempts} 次尝试后沉淀的成功路径，"
                     "供同类任务直接复用。")[:1000],
        status=WorkflowStatus.PUBLISHED,
        created_by=task.created_by, version=1, source="experience",
        owner_agent_id=str(agent.id), reuse_key=reuse_key,
        score=85.0 if attempts >= PUBLISH_MIN_ATTEMPTS else 80.0,
        run_count=1, success_count=1, failure_count=0,
        meta={"experience_id": experience_id, "task_id": str(task.id),
              "task_ids": [str(task.id)], "steps": steps,
              "source_agent_id": str(agent.id), "attempts": attempts})
    db.add(wf)
    await db.flush()

    node_ids: list[str] = []
    start = WorkflowNode(workflow_id=wf.id, node_key="n0",
                         node_type=NodeType.START, name="开始",
                         config={}, position_x=0, position_y=0)
    db.add(start)
    node_ids.append("n0")
    for i, step in enumerate(steps or ["执行任务"], 1):
        key = f"n{i}"
        db.add(WorkflowNode(
            workflow_id=wf.id, node_key=key, node_type=NodeType.AGENT,
            name=f"步骤 {i}：{step[:80]}",
            config={"agent_id": str(agent.id),
                    "task_title_template": task.title[:300],
                    "instruction": step[:1000]},
            position_x=240.0 * i, position_y=0))
        node_ids.append(key)
    end_key = f"n{len(node_ids)}"
    db.add(WorkflowNode(workflow_id=wf.id, node_key=end_key,
                        node_type=NodeType.END, name="完成",
                        config={}, position_x=240.0 * len(node_ids),
                        position_y=0))
    node_ids.append(end_key)
    for a, b in zip(node_ids, node_ids[1:]):
        db.add(WorkflowEdge(workflow_id=wf.id, from_node=a, to_node=b))
    await db.flush()
    return wf


async def harvest_task_experience(db: AsyncSession, task: Task, agent, *,
                                  success: bool, error: str | None = None,
                                  source: str = "runtime") -> dict | None:
    """任务结束后沉淀一条经验；不满足门槛或重复上报时安静返回 None/旧记录。"""
    if task is None or agent is None or not getattr(task, "id", None):
        return None
    executions = (await db.execute(
        select(AgentExecution)
        .where(AgentExecution.task_id == str(task.id))
        .order_by(AgentExecution.created_at))).scalars().all()
    attempts = max(1, len(executions))
    failures = [e for e in executions if (e.status == "failed" or e.error)]
    successes = [e for e in executions
                 if e.status == "completed" and (e.output or "").strip()]
    is_success = bool(success or task.status == TaskStatus.COMPLETED)

    reuse_key = reuse_key_for(task.title)
    if not is_success and attempts < PUBLISH_MIN_ATTEMPTS:
        return None
    if is_success and attempts < PUBLISH_MIN_ATTEMPTS:
        # 一次就成功：只在同类任务完全没有经验时留一条基准路径
        exists = (await db.execute(
            select(func.count()).select_from(SharedExperience)
            .where(SharedExperience.company_id == str(task.company_id),
                   SharedExperience.reuse_key == reuse_key,
                   SharedExperience.status == "published"))).scalar_one()
        if exists:
            return None

    output = (task.result or "").strip()
    if not output and successes:
        output = (successes[-1].output or "").strip()
    last_error = (error or (failures[-1].error if failures else None)
                  or (executions[-1].error if executions else None) or "").strip()

    fallback = _fallback_narrative(task, executions, is_success, output,
                                   last_error or None)
    raw, usage = await _llm_narrative(db, agent, str(task.company_id), task,
                                      executions, is_success, output,
                                      last_error or None)
    narrative = _normalize_narrative(raw, fallback)
    tokens_used = await _task_tokens(db, str(task.id))
    tokens_saved = (tokens_used if not is_success else
                    round(tokens_used * (attempts - 1) / attempts))

    existing = (await db.execute(
        select(SharedExperience)
        .where(SharedExperience.task_id == str(task.id),
               SharedExperience.agent_id == str(agent.id))
        .limit(1))).scalars().first()

    if (existing is not None and existing.kind == "success" and not is_success):
        # 已经沉淀过成功路径，后来的一次失败不能把资产降级
        existing.failure_count = int(existing.failure_count or 0) + 1
        existing.attempts = max(int(existing.attempts or 1), attempts)
        await db.commit()
        return experience_brief(existing)

    row = existing or SharedExperience(
        id=str(uuid.uuid4()), company_id=str(task.company_id),
        agent_id=str(agent.id), task_id=str(task.id),
        task_title=task.title[:500], reuse_key=reuse_key)
    if existing is None:
        db.add(row)

    row.author_agent_name = agent.name
    row.kind = "success" if is_success else "failure"
    row.scope = "company"
    row.status = "published"
    row.summary = narrative["summary"][:1000]
    row.steps = narrative["steps"]
    row.pitfalls = narrative["pitfalls"]
    row.lessons = narrative["lessons"]
    row.token_tips = narrative["token_tips"]
    row.quality_tips = narrative["quality_tips"]
    row.tags = narrative["tags"]
    row.attempts = attempts
    row.success_count = len(successes) or (1 if is_success else 0)
    row.failure_count = len(failures)
    row.tokens_used = tokens_used
    row.tokens_saved_est = max(0, int(tokens_saved))
    row.confidence = (round(min(0.95, 0.6 + 0.1 * (attempts - 1)), 2)
                      if is_success else round(min(0.85, 0.5 + 0.1 * len(failures)), 2))
    row.source = source
    row.meta = {"usage": usage, "reuse_key": reuse_key}
    await db.flush()

    wf = await _ensure_workflow(db, task, agent, reuse_key=reuse_key,
                                steps=narrative["steps"],
                                experience_id=row.id, success=is_success,
                                attempts=attempts)
    if wf is not None:
        row.workflow_id = wf.id
        row.workflow_version = wf.version
    await db.commit()

    try:
        from app.services.event_system import log_event
        await log_event(db, "experience.harvested", "agent", str(agent.id),
                        "system", None,
                        {"experience_id": row.id, "kind": row.kind,
                         "task_id": str(task.id), "attempts": attempts,
                         "workflow_id": row.workflow_id},
                        company_id=str(task.company_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("经验留痕失败 experience=%s: %s", row.id, exc)
    return experience_brief(row)


async def _workflow_steps(db: AsyncSession, wf: WorkflowDefinition | None
                          ) -> list[str]:
    if wf is None:
        return []
    steps = _str_list((wf.meta or {}).get("steps"), limit=6, width=300)
    if steps:
        return steps
    nodes = (await db.execute(
        select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id)
        .order_by(WorkflowNode.position_x, WorkflowNode.node_key))).scalars().all()
    return _str_list([
        str((n.config or {}).get("instruction") or n.name)
        for n in nodes if n.node_type == NodeType.AGENT
    ], limit=6, width=300)


async def build_experience_context(db: AsyncSession, company_id: str,
                                   agent_id: str, task: Task) -> str:
    """任务开工前的经验注入：已验证工作流 + 本 Agent/同事的经验。"""
    if not _env_on("AIEOS_EXPERIENCE_INJECT", True):
        return ""
    reuse_key = reuse_key_for(task.title)
    same_key = (await db.execute(
        select(SharedExperience)
        .where(SharedExperience.company_id == company_id,
               SharedExperience.reuse_key == reuse_key,
               SharedExperience.status == "published")
        .order_by(SharedExperience.confidence.desc(),
                  SharedExperience.created_at.desc())
        .limit(4))).scalars().all()

    colleague = (await db.execute(
        select(SharedExperience)
        .where(SharedExperience.company_id == company_id,
               SharedExperience.status == "published",
               SharedExperience.agent_id != agent_id)
        .order_by(SharedExperience.confidence.desc(),
                  SharedExperience.created_at.desc())
        .limit(3))).scalars().all()

    seen: set[str] = set()
    experiences: list[SharedExperience] = []
    for row in [*same_key, *colleague]:
        if row.id in seen:
            continue
        seen.add(row.id)
        experiences.append(row)

    wf = await latest_workflow(db, company_id, reuse_key)
    if wf is not None and wf.source != "experience":
        wf = None
    steps = await _workflow_steps(db, wf)

    if not experiences and not steps:
        return ""

    sections: list[str] = []
    if steps:
        rate = ""
        if wf is not None and int(wf.run_count or 0):
            rate = (f"，成功率 {int(wf.success_count or 0)}/{int(wf.run_count)}")
        sections.append(
            f"【已验证工作流】{wf.name}（第 {wf.version} 版{rate}）\n"
            "同类任务已经跑通，请按以下步骤执行，不要重新试错：\n"
            + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))

    lines: list[str] = []
    for row in experiences[:5]:
        who = row.author_agent_name or "同事"
        label = "成功路径" if row.kind == "success" else "失败教训"
        lines.append(f"- [{label}] {who}《{row.task_title}》"
                     f"（{row.attempts} 次尝试）：{(row.summary or '')[:300]}")
        if row.kind == "failure" and row.pitfalls:
            lines.append("  踩坑：" + "；".join(_str_list(row.pitfalls, 3, 160)))
        if row.steps and row.kind == "success":
            lines.append("  可复用步骤：" + " → ".join(
                _str_list(row.steps, 4, 120)))
        if row.tokens_used:
            lines.append(f"  上次消耗约 {row.tokens_used} tokens；"
                         f"复用可避免约 {row.tokens_saved_est} tokens 试错")
    if lines:
        sections.append("【团队经验（可直接借鉴）】\n" + "\n".join(lines))

    if not sections:
        return ""
    text = ("以下是公司里同类任务的历史经验。优先复用它；如果发现不适用，"
            "在执行结果里说明原因，不要沉默跳过。\n\n" + "\n\n".join(sections))
    return text[:CONTEXT_CHAR_BUDGET]


async def adopt_experience(db: AsyncSession, row: SharedExperience,
                           agent_id: str, user_id: str | None = None) -> dict:
    from app.models.agent import Agent

    agent = await db.get(Agent, agent_id)
    if agent is None or agent.company_id != row.company_id:
        raise ValueError("目标员工不存在或不属于这家公司")
    adopted = list(row.adopted_by or [])
    if agent_id not in adopted:
        adopted.append(agent_id)
        row.adopted_by = adopted
        await db.commit()
    try:
        from app.memory_engine import MemoryType, remember
        content = (f"学习同事经验（{row.author_agent_name or '同事'}）："
                   f"{row.summary or row.task_title}\n"
                   f"步骤：{' → '.join(_str_list(row.steps, 5, 160))}\n"
                   f"踩坑：{'；'.join(_str_list(row.pitfalls, 3, 160))}")
        await remember(db, agent_id, row.company_id, MemoryType.SEMANTIC,
                       content=content[:2000], importance=0.7,
                       context_ref=f"experience:{row.id}")
    except Exception as exc:  # noqa: BLE001 — 记忆写入失败不回滚采纳动作
        logger.warning("经验写入记忆失败 experience=%s agent=%s: %s",
                       row.id, agent_id, exc)
    try:
        from app.services.event_system import log_event
        await log_event(db, "experience.adopted", "agent", agent_id,
                        "user", user_id,
                        {"experience_id": row.id, "kind": row.kind},
                        company_id=row.company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("经验采纳留痕失败 experience=%s: %s", row.id, exc)
    return experience_brief(row)
