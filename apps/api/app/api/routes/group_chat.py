"""项目/任务群聊 (P4)：老板发言 → 参与的所有 Agent 依次专业反馈。

- 每个 Agent 独立调用：各自知识库(cost_optimized_rag) + web_search 工具
- 后发言的 Agent 能看到先发言 Agent 的内容（互相可见，像开会）
- 消息持久化到 group_chat_messages 表
- ``POST /{scope}/{scope_id}/stream`` 是同一条链路的流式版：老板实时看见
  "谁在想、想的是什么、选了哪个模型、搜了什么、正文逐字出来"
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import JSON, String, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.db.base import Base
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentModelConfig, Model
from app.models.user import User
import re as _re2

logger = get_logger(__name__)


def _parse_text_tool_calls(content: str) -> list[dict]:
    """解析 agnes 等模型以文本形式输出的 <tool_call> 标记。"""
    import json as _json
    import uuid as _uuid
    calls = []
    for m in _re2.finditer(
        r"<tool_call>\s*<function=(\w+)>\s*(.*?)</function>\s*</tool_call>", content, _re2.S):
        name = m.group(1)
        args = {}
        for pm in _re2.finditer(r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", m.group(2), _re2.S):
            args[pm.group(1)] = pm.group(2).strip()
        calls.append({"function": {"name": name,
                                   "arguments": _json.dumps(args)}, "id": f"call_{_uuid.uuid4().hex[:12]}"})
    return calls


router = APIRouter(prefix="/group-chat", tags=["group-chat"])

# 时间戳 mixin
from datetime import datetime as _dt  # noqa: E402
from sqlalchemy import Column, DateTime, func  # noqa: E402


class _TS:
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now())
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())


class GroupChatMessage(Base, _TS):
    """群聊消息。scope: project | task；scope_id 为对应 id。"""
    __tablename__ = "group_chat_messages"
    id = Column(String(36), primary_key=True)
    company_id = Column(String(36), index=True)
    scope = Column(String(10))          # project | task
    scope_id = Column(String(36), index=True)
    agent_id = Column(String(36))       # None = 老板发言
    agent_name = Column(String(200))
    role = Column(String(10))           # boss | agent
    content = Column(JSON)              # {"text": "...", "search_used": bool}


def _now():
    return datetime.now(timezone.utc)


async def _owned_company(db: AsyncSession, user_id: str, company_id: str,
                         capability: Capability = Capability.VIEW):
    """公司门禁：角色具备 capability 才放行（越权 404）。

    company_id 来自调用方，必须先由 resolve_role 确认关系，不能只信参数。
    """
    return await assert_company_capability(db, user_id, company_id, capability)


def _now():
    return datetime.now(_dt.timezone.utc)


import uuid  # noqa: E402


async def _get_member_agents(db, company_id, scope, scope_id):
    """获取参与该 scope 的所有 Agent。"""
    from app.models.project import ProjectMember
    from app.models.chat_group import ChatGroup
    agents = []
    if scope == "project":
        rows = (await db.execute(select(ProjectMember).where(
            ProjectMember.project_id == scope_id,
            ProjectMember.member_type == "agent"))).scalars().all()
        ids = [m.member_id for m in rows]
    elif scope == "group":
        # 自由群聊：成员直接存在 chat_groups.agent_ids
        g = await db.get(ChatGroup, scope_id)
        ids = [str(i) for i in (g.agent_ids or [])] if g and g.company_id == company_id else []
    else:  # task：执行过该任务的 Agent + 指派 Agent
        from app.models.task import Task
        from app.models.task import AgentExecution
        t = await db.get(Task, scope_id)
        ids = []
        if t and getattr(t, "assigned_agent_id", None):
            ids = [t.assigned_agent_id]
        execs = (await db.execute(select(AgentExecution.agent_id).where(
            AgentExecution.task_id == scope_id).distinct())).scalars().all()
        for aid in execs:
            if aid not in ids:
                ids.append(aid)
    if not ids:
        return agents
    rows = (await db.execute(select(Agent).where(
        Agent.id.in_(ids), Agent.company_id == company_id))).scalars().all()
    # SQLite 的 IN 子句不保持入参顺序，按 ids（群成员/任务参与人）的顺序重排，
    # 这样流式输出的先后顺序与群成员顺序一致。
    by_id = {str(a.id): a for a in rows}
    return [by_id[i] for i in ids if i in by_id]


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for real-time information.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
}

# 群聊回复要求 200 字以内，显式 max_tokens=512 避免沿用默认 8192；
# 单次 90s（共享 client 的默认 read=120s，这里再收紧到前端 300s 预算内），
# 仍复用 model_gateway 的共享连接池与超时/重试机制。
GROUP_CHAT_LLM_CONFIG = {"max_tokens": 512, "timeout": 90}
GROUP_CHAT_CONCURRENCY = 4
# 单次群聊同时发言的员工数：5 人群从"每人等 1 分钟"压到约 2 轮。
# 真正调模型的并发上限仍是公司级 GROUP_CHAT_CONCURRENCY。
GROUP_CHAT_PARALLEL = 3
# 任务/话题描述入 prompt 前截断，防止超长需求把上下文撑爆
GROUP_CHAT_TOPIC_LIMIT = 1200

# 按公司限流：同一公司最多 GROUP_CHAT_CONCURRENCY 个群聊发言在跑
# （多老板/多标签页同时发言时不会把模型配额打满）。单进程内有效。
_COMPANY_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _company_semaphore(company_id: str) -> asyncio.Semaphore:
    sem = _COMPANY_SEMAPHORES.get(company_id)
    if sem is None:
        sem = asyncio.Semaphore(GROUP_CHAT_CONCURRENCY)
        _COMPANY_SEMAPHORES[company_id] = sem
    return sem


# 流式群聊：没有事件的那段时间也要保持连接活跃，否则反代/浏览器会把
# "员工还在想" 判成断线。落库的过程精简版与单聊同口径。
GROUP_CHAT_HEARTBEAT_S = 15
MAX_PROCESS_STEPS = 30
MAX_PROCESS_REASONING = 4000

# 流式群聊结束标志（sentinel），端点在线程断掉时也能安全收场
_GROUP_STREAM_DONE = object()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ProcessAccumulator:
    """把转发出去的过程事件攒成能落库的精简版，供刷新后回放。

    群聊的历史接口直接把 message content 平铺返回，所以 process 也放在
    content 里；前端的 processFromMeta 认这个形状（与单聊 meta.process 一致）。
    """

    def __init__(self):
        self.steps: list[dict] = []
        self.reasoning = ""
        self.model: str | None = None
        self.started = time.monotonic()

    def _add(self, kind: str, label: str, status: str = "done",
             detail: str | None = None, step_id: str | None = None) -> None:
        if len(self.steps) >= MAX_PROCESS_STEPS:
            return
        step = {"kind": kind, "label": label[:80], "status": status}
        if detail:
            step["detail"] = detail[:240]
        if step_id:
            step["id"] = step_id
        self.steps.append(step)

    def feed(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "reasoning":
            self.reasoning = (self.reasoning
                              + str(ev.get("text") or ""))[:MAX_PROCESS_REASONING]
        elif kind == "status":
            text = str(ev.get("text") or "").strip()
            if text and (not self.steps
                         or self.steps[-1].get("label") != text[:80]):
                self._add("stage", text)
        elif kind == "model":
            name = str(ev.get("model_name") or ev.get("model_id") or "模型")
            self.model = name
            attempt = ev.get("attempt") or 1
            self._add("model", f"调用模型 · {name}",
                      detail=None if attempt == 1 else f"第 {attempt} 顺位（已降级）")
        elif kind == "model_error":
            name = str(ev.get("model_name") or ev.get("model_id") or "模型")
            self._add("model_error", f"{name} 调用失败", "error",
                      str(ev.get("message") or "")[:240])
        elif kind == "tool_start":
            self._add("tool", str(ev.get("label") or ev.get("name") or "调用工具"),
                      "running", str(ev.get("args") or ""),
                      step_id=str(ev.get("id") or ""))
        elif kind == "tool_end":
            step_id = str(ev.get("id") or "")
            for step in self.steps:
                if step.get("id") == step_id and step["kind"] == "tool":
                    step["status"] = "error" if ev.get("ok") is False else "done"
                    if ev.get("summary"):
                        step["detail"] = (f"{step.get('detail')} · "
                                          f"{ev['summary']}")[:240] if step.get("detail") \
                            else str(ev["summary"])[:240]
                    if isinstance(ev.get("duration_ms"), (int, float)):
                        step["duration_ms"] = int(ev["duration_ms"])
                    break
        elif kind == "delta":
            if not any(s["kind"] == "stage" and s["label"] == "正在生成回答…"
                       for s in self.steps):
                self._add("stage", "正在生成回答…")

    def meta(self) -> dict:
        out: dict = {"duration_ms": int((time.monotonic() - self.started) * 1000)}
        steps = [{k: v for k, v in s.items() if k != "id"} for s in self.steps]
        if steps:
            out["steps"] = steps
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.model:
            out["model"] = self.model
        return out


async def _forward(emit, ev: dict) -> None:
    """有流就转发，没有流（阻塞式 POST）就当无事发生。"""
    if emit is not None:
        await emit(ev)


async def _execute_tool_step(emit, tc: dict, args: dict):
    """跑一次工具调用，并把它播报成一条看得见的步骤。

    搜索这类外部动作动辄几秒，老板看到的应该是「谁在搜什么、搜到几条、花了
    多久」，而不是界面空白地转圈。异常照常往外抛——是否中断由调用方决定，
    但一定要先出声（tool_end ok=false）再走。
    """
    from app.skill_system import execute_skill

    fn = tc.get("function") or {}
    name = str(fn.get("name") or "web_search")
    tool_id = str(tc.get("id") or uuid.uuid4().hex[:12])
    query = str(args.get("query") or "")
    label = (f"联网搜索 · {query[:40]}" if query else f"调用工具 · {name}")
    await _forward(emit, {"type": "tool_start", "id": tool_id, "name": name,
                          "label": label, "args": query})
    started = time.monotonic()
    try:
        res = await execute_skill(name, args)
    except Exception as tool_err:
        await _forward(emit, {
            "type": "tool_end", "id": tool_id, "ok": False,
            "summary": f"{type(tool_err).__name__}: {tool_err}"[:160],
            "duration_ms": int((time.monotonic() - started) * 1000)})
        raise
    hits = (res.get("result") or {}).get("results") or []
    await _forward(emit, {
        "type": "tool_end", "id": tool_id, "ok": True,
        "summary": (f"{len(hits)} 条结果" if hits else "无结果"),
        "duration_ms": int((time.monotonic() - started) * 1000)})
    return res


async def _prepare_default_models(db, agents):
    """未绑过模型的 Agent 挂上公司默认对话模型再开工，只改数据不丢框架。

    没有已配模型的 Agent 会回复 "(未发言)".
    """
    from app.model_discovery import chat_model_condition

    default_model = (await db.execute(
        select(Model.id).where(chat_model_condition())
        .order_by(Model.created_at).limit(1))).scalar_one_or_none()
    for a in agents:
        cfg = (await db.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == str(a.id)))).scalars().first()
        if not cfg and default_model:
            db.add(AgentModelConfig(agent_id=str(a.id), role="primary",
                                    model_id=default_model))
            await db.flush()


async def _llm_round(gw, agent, messages, emit=None):
    """跑一轮 LLM，返回与 chat() 同形状的 {content, tool_calls, finish_reason}。

    给了 emit 就走流式：思考、选了哪个模型、正文逐字出来只有流式才有，
    分片照样攒回来，_agent_speak 后续的剥标记/工具/文档生成不必为流式各写一套。
    流式一轮既没正文也没工具调用时改用非流式补一轮——只有 chat() 带
    finish_reason，才能交给网关判断"是思考吃满预算要抬预算，还是采样踩空"。
    """
    if emit is None:
        return await gw.chat_with_agent_models_by_agent(
            agent, messages, tools=[WEB_SEARCH_TOOL], config=GROUP_CHAT_LLM_CONFIG)
    await emit({"type": "delta_reset"})
    parts: list[str] = []
    done: dict = {}
    async for ev in gw.stream_with_agent_models_by_agent(
            agent, messages, tools=[WEB_SEARCH_TOOL],
            config=GROUP_CHAT_LLM_CONFIG):
        if ev.get("type") == "done":
            done = ev
            continue
        if ev.get("type") == "delta":
            parts.append(ev.get("text") or "")
        await emit(ev)
    content = "".join(parts)
    tool_calls = done.get("tool_calls") or []
    if content.strip() or tool_calls:
        return {"content": content, "tool_calls": done.get("tool_calls"),
                "finish_reason": "stop"}
    await emit({"type": "status", "text": "这轮没吐出内容，换非流式补一轮…"})
    return await gw.chat_with_agent_models_by_agent(
        agent, messages, tools=[WEB_SEARCH_TOOL], config=GROUP_CHAT_LLM_CONFIG)


async def _agent_speak(db, gw, agent, topic, history_lines, emit=None):
    """单个 Agent 发言：知识库 + 可选 web_search + 同事发言可见。

    ``emit`` 给定时全程走流式：思考、模型选择/降级、搜索工具、逐字正文都会
    播报出去；不给时（阻塞式 POST）行为和以前完全一致。
    """
    from app.core.prompting import staff_identity
    from app.kb_engine import cost_optimized_rag
    from app.public_doc_skill import get_public_doc_system_prompt
    import re as _re

    topic = (topic or "").strip()[:GROUP_CHAT_TOPIC_LIMIT]

    messages = [{"role": "system", "content":
        f"你是「{agent.name}」，{agent.role or '数字员工'}。"
        f"{staff_identity(agent.name)}"
        f"现在是项目会议，老板说了想法，请你从你的专业角度给出简短反馈"
        f"（200字以内）。\n"
        f"重要规则：\n"
        f"1. 如果问题涉及实时信息、行业动态、数据、趋势、天气等你不确定的内容，"
        f"必须先调用 web_search 工具搜索，然后基于搜索结果进行总结归纳后回答；\n"
        f"2. 搜索后不要罗列原始结果，要提炼出3-5条核心要点，用你专业的视角解读；\n"
        f"3. 结合知识库内容和搜索结果给出综合判断；\n"
        f"4. 同事的发言你可以引用或补充，避免重复。\n"
        f"5. 当老板要求生成文档（Word/PPT/Excel/MD）时，在回复末尾输出：\n"
        f"[GENERATE_DOC]\n"
        f'{{\"type\":\"word|ppt|excel|md\",\"title\":\"标题\",'
        f'\"content\":\"完整内容，ppt用##分页\"}}\n'
        f"[/GENERATE_DOC]\n"
        f"系统会自动渲染为真实文件。"}]
    try:
        messages.append({"role": "system",
                         "content": get_public_doc_system_prompt(agent.role)})
    except Exception as doc_err:
        logger.warning("群聊公文提示词注入失败 agent=%s: %s", agent.id, doc_err,
                       exc_info=True)
    # Agent 专属文档风格（有技能用专属，无技能用通用格式）
    try:
        from app.models.skill import AgentSkill, Skill as SkillRow
        from app.public_doc_skill import resolve_style, get_bound_style
        _sk_names = (await db.execute(select(SkillRow.name).join(
            AgentSkill, AgentSkill.skill_id == SkillRow.id).where(
            AgentSkill.agent_id == str(agent.id)))).scalars().all()
        custom = await get_bound_style(db, agent)
        style = resolve_style(agent.role, list(_sk_names), custom_style=custom)
        messages.append({"role": "system", "content":
            f"【文档风格要求】使用「{style['style_name']}"
            + ("" if style["is_custom"] else "（该员工未配置专属风格技能，采用通用格式）")
            + "」：" + style["hint"]})
    except Exception as style_err:
        # 之前静默吞掉，导致"专属风格不生效"永远查不出来
        logger.warning("群聊文档风格注入失败 agent=%s: %s", agent.id, style_err,
                       exc_info=True)
    # 知识库
    await _forward(emit, {"type": "status", "text": "正在翻本项目知识库…"})
    try:
        rag = await cost_optimized_rag(db, agent.company_id, str(agent.id),
                                       topic[:300], agent=agent)
        if rag.get("context_text"):
            messages.append({"role": "system",
                             "content": f"相关知识库：\n{rag['context_text'][:2000]}"})
        await _forward(emit, {
            "type": "status",
            "text": ("知识库里找到相关内容" if rag.get("context_text")
                     else "知识库没有相关资料，凭本岗位经验回答")})
    except Exception as kb_err:
        logger.warning("群聊知识库检索失败 agent=%s: %s", agent.id, kb_err,
                       exc_info=True)
        await _forward(emit, {"type": "status",
                              "text": "知识库检索失败，本轮不带知识库资料"})
    # 历史发言（含其他 Agent）
    if history_lines:
        messages.append({"role": "system",
                         "content": "会议已发言记录：\n" + "\n".join(history_lines[-12:])})
    # 实时信息强制搜索
    if any(k in topic for k in ("今天","现在","最新","目前","当前","实时","汇率","天气","温度","新闻","趋势","价格")):
        messages.append({"role": "system", "content":
            "【强制规则】该问题涉及实时/最新信息，你必须调用 web_search 工具，"
            "然后对搜索结果进行总结提炼（3-5条要点）后回答。严禁凭记忆编造数据。"})
    messages.append({"role": "user", "content": topic})

    llm = await _llm_round(gw, agent, messages, emit=emit)
    content = (llm.get("content") or "").strip()
    # agnes-2.5-flash 可能返回空 content 但有 tool_calls，走下方工具调用分支
    if not content and not (llm.get("tool_calls") or []):
        # 空正文不等于"员工没话说"，别直接甩一句 (未发言) 给老板。
        # 但要分清两种空：预算被思考吃满的截断（finish=length）网关已经抬过
        # 预算重发过一次，再原样重发必然还是空，这里不白花第二次钱；
        # 采样踩空（首 token 落空、直接命中停止位）才是同一份上下文重发能救的。
        if llm.get("finish_reason") == "length":
            logger.warning("群聊发言正文被 token 预算截断，网关重发后仍为空 "
                           "agent=%s topic=%s", agent.name, (topic or "")[:60])
            await _forward(emit, {"type": "status",
                                  "text": "思考把这一轮的额度用完了，正文没能生成…"})
        else:
            logger.warning("群聊发言空正文，重发一次 agent=%s finish=%s topic=%s",
                           agent.name, llm.get("finish_reason"), (topic or "")[:60])
            await _forward(emit, {"type": "status",
                                  "text": "这轮没给出内容，同一份上下文重发一次…"})
            llm = await _llm_round(gw, agent, messages, emit=emit)
            content = (llm.get("content") or "").strip()
            if not content and not (llm.get("tool_calls") or []):
                logger.warning("群聊发言重发后仍为空 agent=%s finish=%s topic=%s",
                               agent.name, llm.get("finish_reason"),
                               (topic or "")[:60])
    search_used = False
    tool_calls = llm.get("tool_calls") or []
    # agnes 等模型可能以文本形式输出 <tool_call>（无论是否有结构化字段），一律解析并剥离
    text_calls = _parse_text_tool_calls(content)
    if text_calls:
        for tc in text_calls:
            if not any((t.get("function") or {}).get("arguments") == tc["function"]["arguments"]
                       for t in tool_calls):
                tool_calls.append(tc)
        content = _re2.sub(
            r"<tool_call>\s*<function=\w+>\s*.*?</function>\s*</tool_call>",
            "", content, flags=_re2.S).strip()
    if tool_calls:
        search_summaries = []
        for tc in tool_calls:
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                res = await _execute_tool_step(emit, tc, args)
                hits = (res.get("result") or {}).get("results") or []
                summary = "\n".join(
                    f"- {h.get('title','')}: {(h.get('body') or '')[:150]}"
                    for h in hits[:5])
                if summary:
                    search_used = True
                    q = args.get("query", "")
                    search_summaries.append(f"搜索「{q}」的结果：\n{summary}")
            except Exception as tool_err:
                logger.warning("群聊首轮工具调用失败 tool=%s agent=%s: %s",
                               (tc.get("function") or {}).get("name"),
                               agent.id, tool_err, exc_info=True)
                continue
        if search_summaries:
            # agnes 对标准 tool role 支持不佳——用 user 消息注入搜索结果
            messages.append({"role": "assistant",
                             "content": "我需要先搜索最新信息再回答。"})
            messages.append({"role": "user",
                             "content": "[web_search 搜索结果]\n"
                                        + "\n\n".join(search_summaries)
                                        + "\n\n请基于以上搜索结果总结提炼（3-5条要点），从你的专业角度回答。"})
            await _forward(emit, {"type": "status",
                                  "text": "搜到了，正在基于结果总结…"})
            # 多轮工具调用循环（最多3轮），直到模型给出总结性回答
            for _ in range(3):
                second = await _llm_round(gw, agent, messages, emit=emit)
                s_content = (second.get("content") or "").strip()
                s_struct = second.get("tool_calls") or []
                s_text = _parse_text_tool_calls(s_content)
                if s_struct:
                    # 继续执行结构化工具
                    executed = False
                    for tc in s_struct:
                        try:
                            args2 = json.loads((tc.get("function") or {}).get("arguments", "{}"))
                            r2 = await _execute_tool_step(emit, tc, args2)
                            messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                             "content": str((r2.get("result") or r2))[:1500]})
                            executed = True
                        except Exception as tool_err:
                            logger.warning("群聊续轮工具执行失败 tool=%s agent=%s: %s",
                                           (tc.get("function") or {}).get("name"),
                                           agent.id, tool_err, exc_info=True)
                    if not executed:
                        break
                    continue
                if "<tool_call>" in s_content:
                    clean = _re2.sub(r"<tool_call>\s*<function=\w+>\s*.*?</function>\s*</tool_call>",
                                     "", s_content, flags=_re2.S).strip()
                    for tc in s_text:
                        try:
                            args2 = json.loads(tc["function"]["arguments"])
                            r2 = await _execute_tool_step(emit, tc, args2)
                            res_data = r2.get("result") or {}
                            hits = (res_data.get("results") or [])[:5]
                            summary = "\n".join(
                                f"- {h.get('title','')}: {(h.get('body') or '')[:150]}"
                                for h in hits)
                            messages.append({"role": "user",
                                             "content": f"[搜索结果，请基于此总结]\n{summary}"})
                        except Exception as search_err:
                            logger.warning("群聊搜索结果处理失败 tool=%s agent=%s: %s",
                                           tc.get("function", {}).get("name"),
                                           agent.id, search_err, exc_info=True)
                    if not clean or len(clean) < 20:
                        continue
                    content = clean
                    break
                if s_content:
                    content = s_content
                    break

        # 兜底：搜索结果为空时，用工具调用的原始结果作为回复
        if not content and search_summaries == [] and tool_calls:
            for tc in tool_calls:
                tc_name = (tc.get("function") or {}).get("name", "")
                tc_args = (tc.get("function") or {}).get("arguments", "{}")
                try:
                    args_d = json.loads(tc_args)
                    q = args_d.get("query", "")
                    if q:
                        content = f"关于「{q}」，当前暂无实时数据，请尝试其他问题。"
                        break
                except Exception:
                    logger.warning("组内讨论的工具调用参数不是合法 JSON，跳过该条兜底：%r",
                                   tc_args[:200], exc_info=True)
                    continue
        # 兜底：多轮后仍无总结，把收集到的搜索结果直接总结返回
        if not content or len(content) < 20:
            all_summaries = []
            for m2 in messages:
                if m2.get("role") == "user" and str(m2.get("content", "")).startswith("[搜索结果"):
                    all_summaries.append(str(m2["content"])[:800])
            if all_summaries:
                content = ("基于实时搜索，我了解到以下关键信息：\n\n"
                           + "\n\n".join(all_summaries[-2:])
                           + "\n\n（以上为搜索引擎返回的摘要，供你参考决策。）")
    # 聊天中检测文档标记 → 自动产出真实文件（存Agent交付文件夹）——所有路径都会检查
    try:
        from app.doc_generator import parse_doc_markers as _pdm, generate_document as _gd
        from app.models.asset import Asset as _Asset, AssetType as _AT
        for dspec in _pdm(content):
            gen = _gd(dspec["type"], dspec["title"], dspec["content"],
                      agent_id=str(agent.id))
            db.add(_Asset(company_id=agent.company_id,
                          name=gen["file_name"], type=_AT.OTHER,
                          path=gen["file_path"], mime_type=gen["mime"],
                          size=gen["size"], created_by=str(agent.id),
                          creator_type="agent", agent_id=str(agent.id)))
            content += (f"\n\n📎 已生成交付文件：{gen['file_name']}"
                        f"（{gen['size']//1024}KB，见交付中心/交付文件夹）")
    except Exception as _doc_err:
        logger.warning("群聊文档生成失败 agent=%s: %s", agent.id, _doc_err,
                       exc_info=True)

    return {"text": content or "(未发言)", "search_used": search_used}


class GroupChatIn(BaseModel):
    message: str


@router.post("/{scope}/{scope_id}")
async def group_chat(scope: str, scope_id: str, data: GroupChatIn,
                     company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """老板发言 → 所有参与 Agent 依次反馈（互相可见）。"""
    if scope not in ("project", "task", "group"):
        raise HTTPException(400, "scope must be project, task or group")
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    from app.model_gateway import ModelGateway
    gw = ModelGateway(db)

    # 1. 存老板消息
    db.add(GroupChatMessage(
        id=uuid.uuid4().hex[:32], company_id=company_id, scope=scope,
        scope_id=scope_id, agent_id=None, agent_name=None, role="boss",
        content={"text": data.message}))
    await db.flush()

    # 2. 取参与 Agent + 已有发言记录
    agents = await _get_member_agents(db, company_id, scope, scope_id)
    prev = (await db.execute(select(GroupChatMessage).where(
        GroupChatMessage.company_id == company_id,
        GroupChatMessage.scope == scope,
        GroupChatMessage.scope_id == scope_id).order_by(
        GroupChatMessage.created_at))).scalars().all()
    history = [(m.agent_name or ("我" if m.role == "boss" else "同事"),
                (m.content or {}).get("text", "")) for m in prev]
    # 先把老板消息落库，避免整个群聊期间（含多次 LLM 调用）一直持有写事务
    await db.commit()

    # 3. 依次发言（后说的能看到先说的）；未配置模型的回退全局默认模型。
    # 回退必须限定「能对话」的模型并按登记时间取：这里曾经用 select(Model.id)
    # 的第一行，撞上图片模型后整群成员每轮都 400，且错误只有一句 Bad Request。
    await _prepare_default_models(db, agents)

    replies = []
    sem = _company_semaphore(company_id)
    for ag in agents:
        try:
            # 模型配置（及此前发言）先落库再调 LLM，避免写事务跨越网络等待
            await db.commit()
            # 按公司限制同时进行的群聊 LLM 调用数（含工具续轮）
            async with sem:
                out = await _agent_speak(db, gw, ag, data.message,
                                         [f"{n}: {t}" for n, t in history])
        except Exception as e:
            out = {"text": f"（{ag.name} 发言失败：{e}）", "search_used": False}
        msg = GroupChatMessage(
            id=uuid.uuid4().hex[:32], company_id=company_id, scope=scope,
            scope_id=scope_id, agent_id=str(ag.id), agent_name=ag.name,
            role="agent", content=dict(out))
        db.add(msg)
        # 每个 Agent 的发言单独提交：其余 Agent 立刻可见，也缩短事务
        await db.commit()
        replies.append({"agent_id": str(ag.id), "agent_name": ag.name, **out})
        history.append((ag.name, out["text"]))
    return {"replies": replies}


@router.post("/{scope}/{scope_id}/stream")
async def group_chat_stream(
    scope: str, scope_id: str, data: GroupChatIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """老板发言 → 参与 Agent **并行**反馈（带并发闸门），逐字实时输出。

    与 ``group_chat`` 同一条数据链路（角色、模型、RAG、工具、正文）—— 只
    不再等完整条结果，每条中间状态都转发给老板：

    * ``group_start`` ：参与的所有代理、占比和开始时间
    * ``agent_start``/``agent_done`` ：单个代理的思考/模型/工具/正文
      （其 ``process`` 随 ``tool_end``/``done`` 缀回；`ProcessAccumulator`）。
      并行后每个事件都带 ``agent_id``/``agent_name``，前端按 id 归属气泡，
      ``agent_done`` 按完成先后到达、**不保证**与名单顺序一致。
    * ``group_done``/``error`` ：整轮结束或失败
    * 静止超过 ``GROUP_CHAT_HEARTBEAT_S`` 秒自动补 ``status`` 心跳
    * process 落库在每段 `agent_done` 前完成，`history` 回放同单聊

    并行边界：每个 Agent 用**独立数据库会话**（RAG/技能/用量各写各的），
    总并发受 ``GROUP_CHAT_PARALLEL`` 与公司级 ``_company_semaphore`` 双重
    限制；消息落库按发起顺序写入 ``created_at``，历史回放顺序稳定。

    端点照常放在最后一段，不抢占 ``/{scope}/{scope_id}`` 的短路径。
    """
    if scope not in ("project", "task", "group"):
        raise HTTPException(400, "scope must be project, task or group")
    await _owned_company(db, user.id, company_id, Capability.OPERATE)

    # 1. 存老板消息
    db.add(GroupChatMessage(
        id=uuid.uuid4().hex[:32], company_id=company_id, scope=scope,
        scope_id=scope_id, agent_id=None, agent_name=None, role="boss",
        content={"text": data.message}))
    await db.flush()

    # 2. 取参与 Agent + 已有发言记录（先落库存量数据再流）
    agents = await _get_member_agents(db, company_id, scope, scope_id)
    prev = (await db.execute(select(GroupChatMessage).where(
        GroupChatMessage.company_id == company_id,
        GroupChatMessage.scope == scope,
        GroupChatMessage.scope_id == scope_id).order_by(
        GroupChatMessage.created_at))).scalars().all()
    history = [(m.agent_name or ("我" if m.role == "boss" else "同事"),
                (m.content or {}).get("text", "")) for m in prev]
    await db.commit()

    # 3. 未绑对应对话模型的 Agent 挂上默认模型再开工。
    # 并行后每个 Agent 用独立会话，这些回退配置必须**先提交**，否则它们
    # 在新会话里读不到，等于白挂。
    await _prepare_default_models(db, agents)
    await db.commit()

    agents_payload = [{"agent_id": str(a.id), "agent_name": a.name}
                      for a in agents]
    agent_order = [(str(a.id), a.name) for a in agents]

    async def event_stream():
        q = asyncio.Queue()

        # 正在思考的员工名（并行时可能有多个）
        active: dict = {"names": []}

        async def heartbeat():
            """没新事件时，15 秒补一次 status（心跳）保持连接不断。"""
            while True:
                await asyncio.sleep(GROUP_CHAT_HEARTBEAT_S)
                names = active["names"]
                await q.put({"type": "status",
                             "text": (f"{'、'.join(names[:3])} 仍在思考中，请稍候…"
                                      if names else "进一步汇总中，请稍候…")})

        async def run_agent(idx: int, agent_id: str, agent_name: str,
                            base_ts: datetime) -> None:
            """一个 Agent 的完整发言：独立会话 + 独立过程累加器。

            ``emit`` 给所有事件盖上 ``agent_id``，前端据此把正文/过程写回
            对应气泡——并行时多个 Agent 的 delta 会交错到达，不能再靠
            "最后一个气泡"归属。
            """
            proc = ProcessAccumulator()
            ag = None
            active["names"].append(agent_name)
            q.put_nowait({"type": "agent_start", "agent_id": agent_id,
                          "agent_name": agent_name})

            async def emit_agent(ev: dict) -> None:
                tagged = {**ev, "agent_id": agent_id,
                          "agent_name": agent_name}
                proc.feed(tagged)
                await q.put(tagged)

            try:
                async with AsyncSessionLocal() as adb:
                    from app.model_gateway import ModelGateway
                    ag = await adb.get(Agent, agent_id)
                    if ag is None:
                        raise RuntimeError("员工已不存在")
                    async with _company_semaphore(company_id):
                        # 修复（同事互见）：prompt 此前用**开局快照**构造且
                        # 发言完成后不回写 history——并行下每个 Agent 看到的
                        # 都只有老板那句话，后发言者完全看不到先发言者说了
                        # 什么，与"像开会"的语义直接矛盾。现在在拿到并发
                        # 闸门的那一刻取最新 history 构造，发言完成后把
                        # 本轮发言追加进 history（事件循环内无 await 间隔，
                        # append 无竞态）。下一位拿到闸门的同事即可看见。
                        out = await _agent_speak(
                            adb, ModelGateway(adb), ag, data.message,
                            [f"{n}: {t}" for n, t in history],
                            emit=emit_agent)
                        history.append((agent_name, out["text"]))
            except Exception as e:
                logger.warning("群聊单个 Agent 发言失败 agent=%s: %s",
                               agent_name, e, exc_info=True)
                out = {"text": f"（{agent_name} 发言失败：{e}）",
                       "search_used": False}
            finally:
                if agent_name in active["names"]:
                    active["names"].remove(agent_name)

            # process 必须随正文一起落库，history 才能回放。
            # created_at 按发起序号错开，写入顺序不受完成先后影响。
            async with AsyncSessionLocal() as pdb:
                pdb.add(GroupChatMessage(
                    id=uuid.uuid4().hex[:32], company_id=company_id,
                    scope=scope, scope_id=scope_id, agent_id=agent_id,
                    agent_name=agent_name, role="agent",
                    created_at=base_ts + timedelta(milliseconds=idx),
                    content={"text": out["text"],
                             "search_used": out["search_used"],
                             "process": proc.meta()}))
                await pdb.commit()
            q.put_nowait({"type": "agent_done", "agent_id": agent_id,
                          "agent_name": agent_name, "text": out["text"],
                          "search_used": out["search_used"],
                          "model": proc.model or (ag.role if ag else None)})

        async def agent_loop():
            """并行跑每个 Agent（带并发闸门），事件全部经由 q 输出。"""
            try:
                q.put_nowait({"type": "group_start",
                              "text": f"会议开始（{len(agents)} 名员工参与，并行发言）",
                              "count": len(agents),
                              "agents": agents_payload})
                base_ts = datetime.now(timezone.utc).replace(tzinfo=None)
                par = asyncio.Semaphore(GROUP_CHAT_PARALLEL)

                async def one(idx: int, agent_id: str, agent_name: str):
                    async with par:
                        await run_agent(idx, agent_id, agent_name, base_ts)

                await asyncio.gather(*(
                    one(i, aid, name)
                    for i, (aid, name) in enumerate(agent_order)))
                q.put_nowait({"type": "group_done",
                              "text": "谢谢大家，群聊结束", "count": len(agents)})
                q.put_nowait(_GROUP_STREAM_DONE)
            except Exception as e:
                logger.warning("群聊流式失败: %s", e, exc_info=True)
                q.put_nowait({"type": "error",
                              "message": str(e) or type(e).__name__})
                q.put_nowait(_GROUP_STREAM_DONE)

        sem_task = asyncio.create_task(agent_loop())
        hb = asyncio.create_task(heartbeat())
        try:
            while True:
                ev = await q.get()
                if ev is _GROUP_STREAM_DONE:
                    break
                yield _sse(ev)
        finally:
            sem_task.cancel()
            hb.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@router.get("/{scope}/{scope_id}/history")
async def history(scope: str, scope_id: str,
                  company_id: str = Query(...),
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id)
    rows = (await db.execute(select(GroupChatMessage).where(
        GroupChatMessage.company_id == company_id,
        GroupChatMessage.scope == scope,
        GroupChatMessage.scope_id == scope_id).order_by(
        GroupChatMessage.created_at))).scalars().all()
    return {"messages": [
        {"id": m.id, "role": m.role, "agent_id": m.agent_id,
         "agent_name": m.agent_name, **(m.content or {}),
         "created_at": str(m.created_at)} for m in rows]}
