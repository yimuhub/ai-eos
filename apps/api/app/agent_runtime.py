"""Agent Runtime MVP.

Flow: Load Agent -> Load Model binding -> Load Skills -> Build Context
      -> [Planning Checkpoint] -> Execute via ModelGateway
      -> [Result Checkpoint] -> Save Result -> Update Task.
Pause/Resume via execution_checkpoints (real persisted state).
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.model_gateway import ModelGateway
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.asset import Asset as AssetModel
from app.models.asset import AssetType
from app.models.skill import AgentSkill
from app.models.skill import Skill as SkillRow
from app.models.task import AgentExecution, ExecutionCheckpoint, Task, TaskStatus
from app.skill_system import get_skill_class
from app.task_engine import transition
from app.services.event_system import log_event

logger = get_logger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def build_context(db: AsyncSession, agent: Agent, task: Task) -> tuple[list[dict], list[dict]]:
    """Returns (messages, model_priority_ids)."""
    from app.capability_packs import build_capability_system_prompt, resolve_capabilities

    messages: list[dict] = []
    caps = resolve_capabilities(agent.memory_config)
    base_prompt = agent.system_prompt or (
        f"You are {agent.name}, a professional digital employee"
        + (f" working as {agent.role}" if agent.role else "")
        + ". Complete the assigned task with high quality and respond in Chinese unless told otherwise."
    )
    # 能力包说明拼在人格提示之后，模型知道自己会什么
    cap_prompt = build_capability_system_prompt(agent.memory_config)
    system_prompt = f"{base_prompt}\n\n{cap_prompt}" if cap_prompt else base_prompt
    messages.append({"role": "system", "content": system_prompt})

    # 持久记忆：有能力包时召回相关历史（跨任务经验）
    if "persistent_memory" in caps.packs:
        try:
            from app.memory_engine import recall
            memories = await recall(
                db, str(agent.id),
                query=f"{task.title} {task.description or ''}".strip()[:200])
            if memories:
                mem_lines = "\n".join(
                    f"- [{m.get('type', 'MEM')}] {m.get('content', '')[:150]}"
                    for m in memories[:8])
                messages.append({
                    "role": "system",
                    "content": f"你的长期记忆（相关历史经验）：\n{mem_lines}",
                })
        except Exception:
            pass  # 记忆召回失败不阻塞任务

    # Installed skills described to the model
    result = await db.execute(
        select(SkillRow.slug, SkillRow.name, SkillRow.description)
        .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
        .where(AgentSkill.agent_id == agent.id)
    )
    skills = result.all()
    if skills:
        skill_lines = "\n".join(f"- {n} ({slug}): {d or ''}" for slug, n, d in skills)
        skill_scopes = {}
        for slug, n, d in skills:
            row = await db.execute(select(SkillRow.scope).where(SkillRow.slug == slug))
            sc = row.scalar() or "public"
            skill_scopes[slug] = sc
        scope_lines = "\n".join(
            f"- {n} ({slug}) [{skill_scopes.get(slug, 'public')}]: {d or ''}"
            for slug, n, d in skills)
        messages.append({
            "role": "system",
            "content": f"已安装技能（[public]=公共技能 [exclusive]=专属技能）：\n{scope_lines}\n"
                       f"根据任务需求自行调用所需技能，并在成果中说明使用了哪些技能。",
        })

    # P4: 公文写作能力注入（所有 Agent 都具备 GB/T 9704-2012 排版规范）
    try:
        from app.public_doc_skill import get_public_doc_system_prompt
        doc_prompt = get_public_doc_system_prompt(agent.role)
        messages.append({"role": "system",
                         "content": f"公文写作能力（GB/T 9704-2012）：\n{doc_prompt}"})
    except Exception as doc_err:
        # 公文提示词注入失败不阻塞任务，但需要可见（否则排版能力静默降级）
        logger.warning("公文写作提示词注入失败 agent=%s role=%s: %s",
                       agent.id, agent.role, doc_err, exc_info=True)

    # P4: 成本优化的两级知识库检索（全局公用+Agent私人，分层召回+压缩+缓存+预算）
    try:
        from app.kb_engine import cost_optimized_rag
        query = f"{task.title} {task.description or ''}".strip()[:300]
        rag = await cost_optimized_rag(db, str(task.company_id),
                                       str(agent.id), query, agent=agent)
        if rag.get("context_text"):
            messages.append({
                "role": "system",
                "content": f"相关知识库内容：\n{rag['context_text']}",
            })
            await log_event(db, "kb.rag_used", "task", str(task.id),
                            "system", None,
                            {"hits": rag["hits"],
                             "profile": rag["meta"].get("profile"),
                             "cached": rag.get("from_cache", False),
                             "tokens_saved_est": rag.get("tokens_saved_est", 0)})
    except Exception as kb_err:
        # 知识库失败不阻塞任务，但检索失败会让回答质量静默下降，必须留痕
        logger.warning("知识库检索失败 task=%s agent=%s: %s",
                       task.id, agent.id, kb_err, exc_info=True)

    messages.append({
        "role": "user",
        "content": f"Task: {task.title}\n\nDetails: {task.description or '(none)'}",
    })

    # model priority order: primary -> secondary -> fallback
    cfg_result = await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == agent.id)
    )
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    configs = sorted(cfg_result.scalars().all(), key=lambda c: order.get(c.role, 3))
    model_ids = [c.model_id for c in configs]
    return messages, model_ids


async def write_checkpoint(
    db: AsyncSession, execution_id: str, checkpoint_type: str,
    step_index: int, context_snapshot: dict,
):
    db.add(ExecutionCheckpoint(
        execution_id=execution_id,
        checkpoint_type=checkpoint_type,
        step_index=step_index,
        context_snapshot=context_snapshot,
    ))
    await db.flush()


async def run_agent_on_task(
    db: AsyncSession, agent: Agent, task: Task, actor_type: str = "system"
) -> AgentExecution:
    """Full runtime cycle for one agent-task pair."""
    from app.capability_packs import resolve_capabilities
    caps = resolve_capabilities(agent.memory_config)
    execution = AgentExecution(agent_id=agent.id, task_id=task.id, status="running",
                               input=task.description or task.title)
    db.add(execution)
    await db.flush()

    agent.status = AgentStatus.RUNNING
    if task.status in (TaskStatus.TODO, TaskStatus.PLANNING):
        await transition(db, task, TaskStatus.RUNNING, actor_type=actor_type, commit=False)
    else:
        await db.flush()

    await log_event(db, "agent.started_task", "agent", agent.id, actor_type, agent.id,
                    {"task_id": task.id})

    try:
        messages, model_ids = await build_context(db, agent, task)

        # Planning checkpoint
        await write_checkpoint(db, execution.id, "planning", 0,
                               {"messages": messages, "model_ids": model_ids})

        gateway = ModelGateway(db)
        from app.model_gateway import set_usage_context
        set_usage_context(agent=agent, scenario="TASK",
                          project_id=str(getattr(task, "project_id", None) or "") or None,
                          task_id=str(task.id), user_id=None)
        llm_result = await gateway.chat_with_agent_models(model_ids, messages)

        execution.output = llm_result["content"]
        execution.status = "completed"
        execution.completed_at = utcnow_iso()
        execution.metadata_json = {
            "usage": llm_result.get("usage"),
            "used_model_id": llm_result.get("used_model_id"),
            "raw_model": llm_result.get("raw_model"),
        }

        # Result checkpoint
        await write_checkpoint(db, execution.id, "result", 1,
                               {"output": llm_result["content"][:2000]})

        task.result = llm_result["content"]
        agent.status = AgentStatus.IDLE
        await log_event(db, "agent.completed_task", "agent", agent.id, actor_type, agent.id,
                        {"task_id": task.id})

        # Auto-save result into Asset Center (P1-12)
        try:
            from app.api.routes.assets import save_artifact
            fname = f"{(task.title or 'result')[:60]}.md"
            asset = await save_artifact(db, task.company_id, fname,
                                        llm_result["content"],
                                        task_id=task.id, agent_id=agent.id)
            await db.flush()
            execution.metadata_json = {**(execution.metadata_json or {}),
                                       "asset_id": asset.id}
        except Exception as asset_err:  # asset save must not break the run
            await log_event(db, "asset.autosave_failed", "task", task.id,
                            "agent", agent.id, {"error": str(asset_err)[:300]})

        # P4: 检测 LLM 输出中的文档生成标记 → 渲染真实文件存入 Agent 交付文件夹
        # 修复：原代码引用了未导入的 AssetType，NameError 被 except:pass 吞掉，
        # 导致"LLM 生成了文档标记但文件永不落盘"这一故障完全不可见。
        try:
            from app.doc_generator import generate_document, parse_doc_markers

            doc_specs = parse_doc_markers(llm_result["content"] or "")
            for spec in doc_specs:
                try:
                    gen = generate_document(spec["type"], spec["title"],
                                            spec["content"], agent_id=str(agent.id))
                    asset_doc = AssetModel(
                        company_id=task.company_id,
                        name=gen["file_name"],
                        type=AssetType.OTHER, path=gen["file_path"],
                        mime_type=gen["mime"], size=gen["size"],
                        created_by=str(agent.id), creator_type="agent",
                        task_id=task.id, agent_id=str(agent.id))
                    db.add(asset_doc)
                    await db.flush()
                    await log_event(db, "doc.generated", "task", str(task.id),
                                    "agent", agent.id,
                                    {"type": spec["type"], "title": spec["title"],
                                     "file": gen["file_name"]})
                except Exception as doc_err:
                    # 单个文档失败不阻塞任务，但必须出声（日志 + 事件留痕）
                    logger.warning("文档渲染失败 task=%s type=%s: %s",
                                   task.id, spec.get("type"), doc_err,
                                   exc_info=True)
                    await log_event(db, "doc.generate_failed", "task", str(task.id),
                                    "agent", agent.id,
                                    {"type": spec.get("type"),
                                     "title": spec.get("title"),
                                     "error": f"{type(doc_err).__name__}: {doc_err}"[:300]})
        except Exception as marker_err:
            # 解析层失败（非单文档问题）同样要留痕，否则整块能力会静默消失
            logger.warning("文档标记解析失败 task=%s: %s", task.id, marker_err,
                           exc_info=True)
            await log_event(db, "doc.parse_failed", "task", str(task.id),
                            "agent", agent.id,
                            {"error": f"{type(marker_err).__name__}: {marker_err}"[:300]})

        # 持久记忆 + 自我学习：有能力包时任务结束自动沉淀经验
        if set(caps.packs) & {"persistent_memory", "self_learning"}:
            try:
                from app.memory_engine import MemoryType, remember
                summary = (llm_result["content"] or "").strip()
                if summary:
                    await remember(
                        db, str(agent.id), str(task.company_id),
                        MemoryType.EPISODIC,
                        content=f"完成任务《{task.title}》：{summary[:300]}",
                        importance=0.6,
                        context_ref=str(task.id))
                if "self_learning" in caps.packs:
                    await log_event(db, "agent.self_learning", "agent", agent.id,
                                    "system", None,
                                    {"task_id": task.id,
                                     "note": "auto reflection scheduled"})
            except Exception as mem_err:
                await log_event(db, "agent.memory_write_failed", "task", task.id,
                                "agent", agent.id, {"error": str(mem_err)[:200]})
    except Exception as e:
        execution.status = "failed"
        execution.error = str(e)[:4000]
        execution.completed_at = utcnow_iso()
        agent.status = AgentStatus.ERROR
        await log_event(db, "agent.failed", "agent", agent.id, actor_type, agent.id,
                        {"task_id": task.id, "error": str(e)[:1000]})
        raise
    finally:
        await db.flush()

    return execution


async def pause_execution(db: AsyncSession, execution: AgentExecution) -> AgentExecution:
    execution.status = "paused"
    await write_checkpoint(db, execution.id, "execution", 2, {"paused_at": utcnow_iso()})
    await db.commit()
    return execution


async def resume_execution(db: AsyncSession, execution: AgentExecution) -> AgentExecution:
    """Resume from latest checkpoint."""
    q = await db.execute(
        select(ExecutionCheckpoint)
        .where(ExecutionCheckpoint.execution_id == execution.id)
        .order_by(ExecutionCheckpoint.step_index.desc())
    )
    cp = q.scalars().first()
    snapshot = cp.context_snapshot if cp else {}
    execution.status = "running"
    await log_event(db, "execution.resumed", "execution", execution.id, "system", None,
                    {"from_checkpoint": cp.checkpoint_type if cp else None})
    await db.commit()
    return execution
