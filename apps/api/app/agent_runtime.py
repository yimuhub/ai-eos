"""Agent Runtime MVP.

Flow: Load Agent -> Load Model binding -> Load Skills -> Build Context
      -> [Planning Checkpoint] -> Execute via ModelGateway
      -> [Result Checkpoint] -> Save Result -> Update Task.
Pause/Resume via execution_checkpoints (real persisted state).
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.model_gateway import ModelGateway
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.asset import Asset as AssetModel
from app.models.asset import AssetType
from app.models.skill import AgentSkill
from app.models.skill import Skill as SkillRow
from app.models.task import AgentExecution, ExecutionCheckpoint, Task, TaskStatus
from app.task_engine import transition
from app.services.event_system import log_event

logger = get_logger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def build_context(db: AsyncSession, agent: Agent, task: Task,
                        extra_system: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (messages, model_priority_ids).

    ``extra_system`` 是调用方注入的额外系统段（CEO 准绳、任务缺口说明等）。
    紧随人格提示之后、用户消息之前：细节可以被补充，但红线不会被后来的
    用户消息覆盖——这正是「一根准绳」在上下文里的落点。
    """
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
    for block in (extra_system or []):
        if block and block.strip():
            messages.append({"role": "system", "content": block})

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
        except Exception as mem_recall_err:
            # 记忆召回失败不阻塞任务，但必须留痕（否则静默降级）
            logger.warning("记忆召回失败，任务继续 task=%s agent=%s: %s",
                           task.id, agent.id, mem_recall_err, exc_info=True)

    # Installed skills described to the model
    result = await db.execute(
        select(SkillRow.slug, SkillRow.name, SkillRow.description,
               SkillRow.scope, SkillRow.input_schema)
        .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
        .where(AgentSkill.agent_id == agent.id)
    )
    skills = result.all()
    if skills:
        scope_lines = "\n".join(
            f"- {n} ({slug}) [{(sc or 'public')}]: {d or ''}"
            for slug, n, d, sc, _schema in skills)
        messages.append({
            "role": "system",
            "content": f"已安装技能（[public]=公共技能 [exclusive]=专属技能）：\n{scope_lines}\n"
                       f"根据任务需求自行调用所需技能，并在成果中说明使用了哪些技能。",
        })
        # 技能卡里的方法论必须真正进提示词：只列技能名等于"挂了张证书不上岗"。
        # 每个技能截断、总量截断，避免把上下文预算吃光。
        methods = []
        for slug, name, _d, _sc, schema in skills:
            instruction = ""
            if isinstance(schema, dict):
                instruction = str(schema.get("instruction") or "").strip()
            if instruction:
                methods.append(f"### {name}（{slug}）\n{instruction[:1500]}")
        if methods:
            messages.append({
                "role": "system",
                "content": ("技能执行方法论（按步骤执行，不要跳过）：\n\n"
                            + "\n\n".join(methods))[:6000],
            })

    # 跨 Agent 学习：同类任务的已验证工作流 + 本 Agent/同事的成功与失败经验。
    # 只注入提示词，不自动改流程；失败不阻塞开工（经验是增强项）。
    try:
        from app.experience_engine import build_experience_context
        experience = await build_experience_context(
            db, str(task.company_id), str(agent.id), task)
        if experience:
            messages.append({"role": "system", "content": experience})
    except Exception as exp_err:
        logger.warning("经验注入失败，任务继续 task=%s agent=%s: %s",
                       task.id, agent.id, exp_err, exc_info=True)

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

    # 工具使用纪律：与实际下发的 tools schema 配套（见 app/tools.py）
    from app.tools import tool_system_prompt
    messages.append({"role": "system", "content": tool_system_prompt()})

    # 报告类任务注入专业骨架：章节、数据表、出处、真实文件交付
    try:
        from app.report_templates import build_report_instruction, detect_report_template
        tpl = detect_report_template(task.title, task.description)
        if tpl:
            messages.append({"role": "system",
                             "content": build_report_instruction(tpl)})
    except Exception as tpl_err:
        logger.warning("报告模板注入失败 task=%s: %s", task.id, tpl_err,
                       exc_info=True)

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
    db: AsyncSession, agent: Agent, task: Task, actor_type: str = "system",
    takeover_note: str | None = None,
    extra_system: list[str] | None = None,
) -> AgentExecution:
    """Full runtime cycle for one agent-task pair.

    V5.5 B1：takeover_note = 老板接管时给的改方向指令，
    恢复执行时作为最高优先级系统指令注入。
    extra_system：CEO 准绳等调用方注入的系统段（见 build_context）。
    """
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
        messages, model_ids = await build_context(db, agent, task,
                                                  extra_system=extra_system)
        # V5.5 B1：老板接管指令（最高优先级，放在所有上下文之后）
        if takeover_note:
            messages.append({
                "role": "system",
                "content": (f"⚠ 老板接管指令（最高优先级，覆盖之前的执行方向）：\n{takeover_note}\n"
                            "请严格按此指令调整后续执行，并先在执行结果中说明你是如何响应该指令的。"),
            })

        # Planning checkpoint
        await write_checkpoint(db, execution.id, "planning", 0,
                               {"messages": messages, "model_ids": model_ids})

        # 短事务边界（P0-6）：execution/task/agent 三行到这里已经 flush，
        # 拿着的行锁必须**在调 LLM 之前**释放。原实现一路持有写事务跨过一次
        # 几秒到几分钟的网络调用，SQLite 下（整库锁）会把所有其它请求堵死，
        # 而 PG 下则是长事务 + 长行锁。先 commit，拿到结果后开新事务写回复。
        await db.commit()

        gateway = ModelGateway(db)
        from app.model_gateway import set_usage_context
        set_usage_context(agent=agent, scenario="TASK",
                          project_id=str(getattr(task, "project_id", None) or "") or None,
                          task_id=str(task.id), user_id=None)

        # ---- 工具循环：任务链过去只做一次 LLM 调用，模型手里没有任何工具，
        # "自动抓数据"只能靠编。现在把 web_search / web_fetch 真正下发，
        # 并在有 tool_calls 时执行、回填、再问，直到模型给出最终回答。
        from app.tools import (MAX_TOOL_CALLS, MAX_TOOL_RESULT_CHARS,
                               MAX_TOOL_ROUNDS, TOOLS as AGENT_TOOLS,
                               execute_tool, parse_text_tool_calls,
                               strip_text_tool_calls)

        llm_result = await gateway.chat_with_agent_models(
            model_ids, messages, tools=AGENT_TOOLS)
        # 网关已在会话里写下用量流水：立刻提交，别让写事务跨工具轮持有
        # SQLite 写锁——并发的工作台对话落库会 busy_timeout 15s 超时报
        # "database is locked"（测评 B-080 的另一半根因）。
        await db.commit()
        tool_trace: list[dict] = []
        total_calls = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            calls = list(llm_result.get("tool_calls") or [])
            content = llm_result.get("content") or ""
            # 兼容把 tool_call 写成文本的模型（否则工具永远不会被执行）
            text_calls = parse_text_tool_calls(content)
            seen = {(c.get("function") or {}).get("name", "") +
                    str((c.get("function") or {}).get("arguments", "")) for c in calls}
            for tc in text_calls:
                key = tc["function"]["name"] + str(tc["function"]["arguments"])
                if key not in seen:
                    calls.append(tc)
                    seen.add(key)
            if not calls:
                break
            if text_calls:
                content = strip_text_tool_calls(content)

            messages.append({"role": "assistant", "content": content,
                             "tool_calls": calls})
            for tc in calls:
                if total_calls >= MAX_TOOL_CALLS:
                    break
                fname = (tc.get("function") or {}).get("name", "")
                raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                res = await execute_tool(fname, args,
                                         company_id=str(task.company_id),
                                         agent_id=str(agent.id))
                total_calls += 1
                ok = "error" not in res
                tool_trace.append({
                    "name": fname, "ok": ok,
                    "arg": str(args.get("query") or args.get("url") or "")[:200],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": json.dumps(res, ensure_ascii=False,
                                          default=str)[:MAX_TOOL_RESULT_CHARS],
                })
            llm_result = await gateway.chat_with_agent_models(
                model_ids, messages, tools=AGENT_TOOLS)
            # 同上：每轮工具调用后立即释放写事务（含上一轮网关用量流水）
            await db.commit()
            rounds += 1

        # 文本形式的残留标记不能进正文/正文落盘文件
        if isinstance(llm_result.get("content"), str):
            llm_result["content"] = strip_text_tool_calls(llm_result["content"])
        if tool_trace:
            await log_event(db, "agent.tools_used", "task", str(task.id),
                            "agent", agent.id,
                            {"count": total_calls,
                             "tools": [t["name"] for t in tool_trace][:20],
                             "ok": sum(1 for t in tool_trace if t["ok"])})

        # LLM 期间任务可能已被取消/暂停：重读最新状态。
        # 取消要"立即生效"，成果不再写回任务（执行记录仍保留，便于审计）。
        await db.refresh(task)
        if task.status == TaskStatus.CANCELLED:
            execution.status = "cancelled"
            execution.completed_at = utcnow_iso()
            await log_event(db, "agent.cancelled_after_llm", "agent", agent.id,
                            actor_type, agent.id,
                            {"task_id": task.id,
                             "note": "LLM 返回时任务已取消，成果未写回"})
            await db.commit()
            return execution

        # 暂停要同样"立即生效"（修复前只查 CANCELLED）：LLM 返回时任务已被
        # 用户暂停的话，把结果存进 checkpoint 就停——不覆盖 execution 的
        # paused 状态、不写 task.result、不把 Agent 置回空闲。否则会出现
        # "任务 PAUSED → 恢复时找不到 paused 执行 → 永久 RUNNING 无 worker"
        # 的假恢复死路（resume 只认 status=='paused' 的执行行）。
        if task.status == TaskStatus.PAUSED:
            await write_checkpoint(db, execution.id, "llm_result", 1,
                                   {"output": llm_result["content"][:2000],
                                    "usage": llm_result.get("usage"),
                                    "used_model_id": llm_result.get("used_model_id")})
            await log_event(db, "agent.paused_after_llm", "agent", agent.id,
                            actor_type, agent.id,
                            {"task_id": task.id,
                             "note": "LLM 返回时任务已暂停，结果存检查点，恢复后直接采用"})
            await db.commit()
            return execution

        # 写回前对执行行做 CAS：只有仍是 running 才允许置 completed。
        # （与 pause_execution 的 CAS 对应；竞态下宁可少写一次成果，
        # 也不能把刚被置为 paused 的执行覆盖成 completed。）
        refreshed = await db.execute(
            update(AgentExecution)
            .where(AgentExecution.id == execution.id,
                   AgentExecution.status == "running")
            .values(status="completed", completed_at=utcnow_iso(),
                    output=llm_result["content"],
                    metadata_json={
                        "usage": llm_result.get("usage"),
                        "used_model_id": llm_result.get("used_model_id"),
                        "raw_model": llm_result.get("raw_model"),
                        "tools_used": tool_trace,
                    }))
        if refreshed.rowcount == 0:
            # 执行状态已被其他路径改掉（paused/cancelled/superseded）：本轮作废
            await log_event(db, "agent.write_back_lost_race", "agent", agent.id,
                            actor_type, agent.id,
                            {"task_id": task.id,
                             "note": "写回时执行状态已非 running，成果未落库"})
            await db.commit()
            return execution
        await db.refresh(execution)

        execution.output = llm_result["content"]
        execution.status = "completed"
        execution.completed_at = utcnow_iso()
        execution.metadata_json = {
            "usage": llm_result.get("usage"),
            "used_model_id": llm_result.get("used_model_id"),
            "raw_model": llm_result.get("raw_model"),
            "tools_used": tool_trace,
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

        # 成功经验沉淀：同类任务下次直接复用这条路径，而不是重新试错。
        try:
            from app.experience_engine import harvest_task_experience
            await db.flush()
            await harvest_task_experience(db, task, agent, success=True)
        except Exception as exp_err:  # noqa: BLE001 — 沉淀失败不能改判任务结果
            logger.warning("成功经验沉淀失败 task=%s agent=%s: %s",
                           task.id, agent.id, exp_err, exc_info=True)
    except Exception as e:
        execution.status = "failed"
        execution.error = str(e)[:4000]
        execution.completed_at = utcnow_iso()
        agent.status = AgentStatus.ERROR
        try:
            await log_event(db, "agent.failed", "agent", agent.id, actor_type, agent.id,
                            {"task_id": task.id, "error": str(e)[:1000]})
            # 失败证据必须**先落库，再往上抛**。
            #
            # 调用方（api/routes/tasks._run_in_background）为了把任务从 RUNNING
            # 清理到 FAILED 会执行 session.rollback()。如果失败原因还停留在
            # 未提交事务里，就会被一并撤销——用户看到"任务失败"，点进执行记录
            # 却什么都没有，错误只存在于服务端日志里。
            # 这正是端到端试跑抓到的缺陷：/tasks/{id}/executions 返回空数组。
            await db.commit()
        except Exception as persist_err:
            # 证据落库本身失败也不能掩盖原始错误，两个都要留痕
            await db.rollback()
            logger.error("失败证据落库失败 task=%s agent=%s 原始错误=%s: %s",
                         task.id, agent.id, e, persist_err, exc_info=True)

        # 失败教训沉淀：至少两次尝试才值得记，单次抖动不写资产。
        try:
            from app.experience_engine import harvest_task_experience
            await harvest_task_experience(db, task, agent, success=False,
                                          error=str(e))
        except Exception as exp_err:  # noqa: BLE001 — 沉淀失败不能掩盖原始错误
            logger.warning("失败经验沉淀失败 task=%s agent=%s: %s",
                           task.id, agent.id, exp_err, exc_info=True)
        raise
    finally:
        await db.flush()

    return execution


async def pause_execution(db: AsyncSession, execution: AgentExecution) -> AgentExecution:
    """暂停一次执行（CAS：只有 running→paused 的那一次会写检查点）。"""
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    res = await db.execute(
        update(AgentExecution)
        .where(AgentExecution.id == execution.id,
               AgentExecution.status == "running")
        .values(status="paused")
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        # 已经被别的请求暂停/取消了：不重复写检查点。
        # 不回滚事务：调用方可能在同一事务里还持有别的对象，回滚会连带失效。
        return execution
    set_committed_value(execution, "status", "paused")
    await write_checkpoint(db, execution.id, "execution", 2, {"paused_at": utcnow_iso()})
    await db.commit()
    return execution


async def resume_execution(db: AsyncSession, execution: AgentExecution) -> AgentExecution:
    """从最新 checkpoint 真正恢复：重新提交后台工作，而不是只翻状态。

    旧实现只把 execution.status 置回 running：如果暂停发生在 LLM 调用中，
    旧调用结束后尝试 PAUSED→COMPLETED 非法转移被外层吞掉，任务永久卡死，
    且没有任何 worker 在跑（假恢复）。这里把旧执行标记为 superseded，
    另起一次真正的后台执行（checkpoint 类型记入事件，保证可追溯）。

    并发保护：paused→superseded 是 CAS，双击「继续」只有一次会真正 submit，
    否则同一个执行会被续跑两次（重复计费）。
    """
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    q = await db.execute(
        select(ExecutionCheckpoint)
        .where(ExecutionCheckpoint.execution_id == execution.id)
        .order_by(ExecutionCheckpoint.step_index.desc())
    )
    cp = q.scalars().first()
    from_checkpoint = cp.checkpoint_type if cp else None
    # V5.5 B1：若最近是老板接管检查点，取出指令注入恢复执行
    takeover_note: str | None = None
    if cp and cp.checkpoint_type == "human_takeover":
        takeover_note = (cp.context_snapshot or {}).get("note")
    if not takeover_note:
        # 兜底：找该执行最近一个 human_takeover（跨步骤）
        q2 = await db.execute(
            select(ExecutionCheckpoint)
            .where(ExecutionCheckpoint.execution_id == execution.id,
                   ExecutionCheckpoint.checkpoint_type == "human_takeover")
            .order_by(ExecutionCheckpoint.step_index.desc()))
        cp2 = q2.scalars().first()
        if cp2:
            takeover_note = (cp2.context_snapshot or {}).get("note")

    res = await db.execute(
        update(AgentExecution)
        .where(AgentExecution.id == execution.id,
               AgentExecution.status == "paused")
        .values(status="superseded", completed_at=utcnow_iso())
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        # 已经被别的请求恢复过：不再重复提交后台工作。
        # 不回滚事务：调用方可能在同一事务里还持有别的对象，回滚会连带失效。
        return execution
    set_committed_value(execution, "status", "superseded")
    await log_event(db, "execution.resumed", "execution", execution.id, "system", None,
                    {"from_checkpoint": from_checkpoint,
                     "has_takeover_note": bool(takeover_note),
                     "note": "旧执行 superseded，重新提交后台工作真正恢复"})
    await db.commit()

    from app.background_jobs import submit_async
    agent_id, task_id, execution_id = execution.agent_id, execution.task_id, execution.id

    async def _resume_job(session):
        from app.models.task import TaskStatus as TS
        agent = await session.get(Agent, agent_id)
        task = await session.get(Task, task_id)
        if not agent or not task:
            logger.error("恢复执行失败：agent/task 不存在 execution=%s", execution_id)
            return
        try:
            new_exec = await run_agent_on_task(session, agent, task,
                                               actor_type="system",
                                               takeover_note=takeover_note)
            if task.status == TS.RUNNING:
                await transition(session, task, TS.COMPLETED,
                                 actor_type="agent", actor_id=agent_id)
            else:
                # 恢复期间任务状态又被人工干预（如再次暂停）：尊重最新状态，不硬转
                logger.warning("恢复执行完成但任务状态已变为 %s，不自动转 COMPLETED task=%s",
                               task.status, task_id)
                await session.commit()
        except Exception as exc:
            # 与 tasks._run_in_background 同策略：失败证据落库 + 任务标 FAILED
            logger.error("恢复执行失败 execution=%s task=%s: %s",
                         execution_id, task_id, exc, exc_info=True)
            await session.rollback()
            t2 = await session.get(Task, task_id)
            failed_now = False
            if t2 and t2.status == TS.RUNNING:
                t2.status = TS.FAILED
                failed_now = True
            if failed_now:
                from app.services.notification_service import notify_task_terminal
                await notify_task_terminal(
                    session, t2, "FAILED",
                    error=f"{type(exc).__name__}: {exc}")
            try:
                await log_event(session, "task.failed", "task", task_id,
                                "system", None,
                                {"agent_id": agent_id, "resumed_from": execution_id,
                                 "error": f"{type(exc).__name__}: {exc}"[:800]})
                await session.commit()
            except Exception as mark_err:
                await session.rollback()
                logger.error("恢复失败状态落库失败 execution=%s: %s",
                             execution_id, mark_err, exc_info=True)
            return
        await log_event(session, "execution.resumed_done", "execution", new_exec.id,
                        "system", None, {"supersedes": execution_id,
                                         "from_checkpoint": from_checkpoint})
        await session.commit()

    # 队满时不能假装恢复成功：把 submit_async 的结果交回调用方处理
    # （QUEUE_FULL 时调用方应回滚任务状态 RUNNING→PAUSED），否则任务已
    # RUNNING、执行已 superseded、又没有 worker——僵尸任务（修复前丢弃返回值）。
    submit_result = submit_async(_resume_job)
    return execution, submit_result
