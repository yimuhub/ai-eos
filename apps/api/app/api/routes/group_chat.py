"""项目/任务群聊 (P4)：老板发言 → 参与的所有 Agent 依次专业反馈。

- 每个 Agent 独立调用：各自知识库(cost_optimized_rag) + web_search 工具
- 后发言的 Agent 能看到先发言 Agent 的内容（互相可见，像开会）
- 消息持久化到 group_chat_messages 表
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import JSON, String, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.db.base import Base
from app.models.agent import Agent, Model
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
    return list(rows)


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
# 推理型模型的 max_tokens 是"思考 + 正文"一起算的：实测 Sage-A1.5 会把 512
# 全花在思考上（finish_reason=length、正文 0 字），看起来就是"员工没说话"。
# 这种截断原样重发必然还是空，所以重试时把预算抬到这一档。
# 2048 不够——同一位员工抬到 2048 后仍是 length（思考本身就要 2000+），
# 4096 实测能吐出正文/工具调用。只在重发那一轮抬，正常轮仍是 512。
GROUP_CHAT_RETRY_MAX_TOKENS = 4096
GROUP_CHAT_CONCURRENCY = 4
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


async def _agent_speak(db, gw, agent, topic, history_lines):
    """单个 Agent 发言：知识库 + 可选 web_search + 同事发言可见。"""
    from app.kb_engine import cost_optimized_rag
    from app.public_doc_skill import get_public_doc_system_prompt
    import re as _re

    topic = (topic or "").strip()[:GROUP_CHAT_TOPIC_LIMIT]

    messages = [{"role": "system", "content":
        f"你是「{agent.name}」，{agent.role or '数字员工'}。"
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
    try:
        rag = await cost_optimized_rag(db, agent.company_id, str(agent.id),
                                       topic[:300], agent=agent)
        if rag.get("context_text"):
            messages.append({"role": "system",
                             "content": f"相关知识库：\n{rag['context_text'][:2000]}"})
    except Exception as kb_err:
        logger.warning("群聊知识库检索失败 agent=%s: %s", agent.id, kb_err,
                       exc_info=True)
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

    cfg = dict(GROUP_CHAT_LLM_CONFIG)
    llm = await gw.chat_with_agent_models_by_agent(
        agent, messages, tools=[WEB_SEARCH_TOOL], config=cfg)
    content = (llm.get("content") or "").strip()
    # agnes-2.5-flash 可能返回空 content 但有 tool_calls，走下方工具调用分支
    if not content and not (llm.get("tool_calls") or []):
        # 空正文不等于"员工没话说"，别直接甩一句 (未发言) 给老板。
        # 被 max_tokens 截断的（思考占满预算）要抬预算再发一次——原样重发
        # 一定还是空；非截断的空才当作采样踩空，用同一份上下文重发。
        finish = llm.get("finish_reason")
        if finish == "length" and cfg.get("max_tokens", 0) < GROUP_CHAT_RETRY_MAX_TOKENS:
            cfg["max_tokens"] = GROUP_CHAT_RETRY_MAX_TOKENS
        logger.warning("群聊发言空正文，重试一次 agent=%s finish=%s max_tokens=%s topic=%s",
                       agent.name, finish, cfg.get("max_tokens"), (topic or "")[:60])
        llm = await gw.chat_with_agent_models_by_agent(
            agent, messages, tools=[WEB_SEARCH_TOOL], config=cfg)
        content = (llm.get("content") or "").strip()
        if not content and not (llm.get("tool_calls") or []):
            logger.warning("群聊发言重试后仍为空 agent=%s finish=%s topic=%s",
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
                from app.skill_system import execute_skill
                res = await execute_skill(tc["function"]["name"], args)
                hits = (res.get("result") or {}).get("results") or []
                summary = "\n".join(
                    f"- {h.get('title','')}: {(h.get('body') or '')[:150]}"
                    for h in hits[:5])
                if summary:
                    search_used = True
                    q = args.get("query", "")
                    search_summaries.append(f"搜索「{q}」的结果：\n{summary}")
            except Exception:
                continue
        if search_summaries:
            # agnes 对标准 tool role 支持不佳——用 user 消息注入搜索结果
            messages.append({"role": "assistant",
                             "content": "我需要先搜索最新信息再回答。"})
            messages.append({"role": "user",
                             "content": "[web_search 搜索结果]\n"
                                        + "\n\n".join(search_summaries)
                                        + "\n\n请基于以上搜索结果总结提炼（3-5条要点），从你的专业角度回答。"})
            # 多轮工具调用循环（最多3轮），直到模型给出总结性回答
            for _ in range(3):
                second = await gw.chat_with_agent_models_by_agent(
                    agent, messages, tools=[WEB_SEARCH_TOOL], config=cfg)
                s_content = (second.get("content") or "").strip()
                s_struct = second.get("tool_calls") or []
                s_text = _parse_text_tool_calls(s_content)
                if s_struct:
                    # 继续执行结构化工具
                    executed = False
                    for tc in s_struct:
                        try:
                            args2 = json.loads((tc.get("function") or {}).get("arguments", "{}"))
                            from app.skill_system import execute_skill as _es
                            r2 = await _es(tc["function"]["name"], args2)
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
                            from app.skill_system import execute_skill as _es
                            r2 = await _es(tc["function"]["name"], args2)
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
    from app.models.agent import AgentModelConfig
    from app.model_discovery import chat_model_condition
    default_model = (await db.execute(
        select(Model.id).where(chat_model_condition())
        .order_by(Model.created_at).limit(1))).scalar_one_or_none()

    async def _ensure_model(a):
        cfg = (await db.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == str(a.id)))).scalars().first()
        if not cfg and default_model:
            db.add(AgentModelConfig(agent_id=str(a.id), role="primary",
                                    model_id=default_model))
            await db.flush()

    replies = []
    sem = _company_semaphore(company_id)
    for ag in agents:
        try:
            await _ensure_model(ag)
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
