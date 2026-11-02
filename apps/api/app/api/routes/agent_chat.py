"""老板 ↔ Agent 一对一直聊 (P4)。

- POST /{aid}/chat: 老板发消息 → Agent 基于自身记忆/知识库/技能用 LLM 回复
  意图识别：
  * 安排任务给该 Agent → 创建 Task 并启动
  * 指挥安排其他 Agent → 创建 Task 指派给目标 Agent
  * 普通问答 → 结合长期记忆+知识库直接回答
- GET /{aid}/chat/history: 对话历史
对话记录持久化到 agent_chat_messages 表（新表）。
"""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.ceo_companion import DEFAULT_THREAD_TITLE, ensure_default_thread
from app.core.chat_attachments import (ChatAttachment,
                                       build_attachments_context,
                                       resolve_attachments)
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.core.scope import ensure_agent_access
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.agent_chat import AgentChatMessage
from app.models.ceo_thread import CeoThread

logger = get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agent-chat"])

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时信息，返回标题、链接、摘要。"
                       "涉及外部事实、天气、价格、新闻、政策等实时数据时必须先搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "Number of results (1-10)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "抓取网页正文纯文本。搜索结果只有摘要时，"
                       "对权威来源（如 weather.com.cn / nmc.cn）取正文核对数字。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 地址"},
                "max_chars": {"type": "integer", "default": 6000},
            },
            "required": ["url"],
        },
    },
}


COMPUTER_TOOLS = [
    {"type": "function", "function": {"name": "computer_screenshot", "description": "Take a screenshot. No params needed.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "computer_run_shell", "description": "Run a read-only shell command (ls/cat/pwd/find/...). Requires run_shell permission.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "computer_run_write", "description": "Run a write shell command (mv/cp/mkdir/rm/touch). Requires run_write permission. Use --dry-run first.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "computer_click", "description": "Click at (x, y) on screen.", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {"name": "computer_type_text", "description": "Type text into focused field.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "computer_run_app", "description": "Launch an application.", "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}},
]

def _parse_text_tool_calls(content: str) -> list[dict]:
    """解析 agnes 等模型以文本形式输出的 <tool_call> 标记。"""
    calls = []
    for m in re.finditer(
        r"<tool_call>\s*<function=(\w+)>\s*(.*?)</function>\s*</tool_call>", content, re.S):
        name = m.group(1)
        args = {}
        for pm in re.finditer(r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", m.group(2), re.S):
            args[pm.group(1)] = pm.group(2).strip()
        calls.append({"function": {"name": name,
                                   "arguments": json.dumps(args)}, "id": f"call_{uuid.uuid4().hex[:12]}"})
    return calls


async def execute_skill_safe(db, fname, args, *, company_id: str | None = None,
                             agent_id: str | None = None):
    """执行一个工具调用。网络类工具统一走 app.tools.execute_tool。

    execute_tool 里 web_fetch 会复用 browser_agent 的 SSRF 闸门并写审计日志；
    这里把 company_id/agent_id 一并传下去，审计记录才带得上租户与 Agent。
    """
    from app.tools import TOOL_NAMES, execute_tool

    if fname in TOOL_NAMES:
        return await execute_tool(fname, args if isinstance(args, dict) else {},
                                  company_id=company_id, agent_id=agent_id)
    from app.skill_system import execute_skill
    res = await execute_skill(fname, args)
    return res.get("result") or res


async def _computer_action(db: AsyncSession, capability: str, args: dict,
                           user_id: str, company_id: str) -> dict:
    """聊天中调用 computer_* 工具时的统一入口。

    ## 改造说明（这是一个真实存在但从未定义的函数）

    改造前本文件的 tool_call 分支调用 `_computer_action(...)`，
    但该函数在整个代码库中**只有调用、没有定义**（pyflakes: undefined name）。
    NameError 被外层 `except Exception` 吞掉，表现为"电脑操作功能没反应"。

    危险之处在于：如果只是顺手补一个直连 executor 的实现，
    就等于给 LLM 开了一条**绕过 ComputerPermission 权限模型**、
    直通主机 shell 的通道。因此这里强制走 computer_service——
    未授权能力一律拒绝，且每次调用（含被拒绝的）都落审计日志。
    """
    from app.services.computer_service import (
        ComputerActionDenied,
        execute_computer_action,
    )

    logger.info("聊天触发本机操作 user=%s company=%s capability=%s",
                user_id, company_id, capability)

    try:
        outcome = await execute_computer_action(
            db, company_id=company_id, capability=capability,
            params=args if isinstance(args, dict) else {})
    except ComputerActionDenied as exc:
        # 给模型一句可读的拒绝原因，让它能向老板解释而不是"操作失败"
        return {"ok": False, "denied": True, "capability": capability,
                "error": f"{exc.reason}。如需使用，请先在工作台「本机能力授权」中开启。"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return outcome["result"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None


async def _visible_agent(db: AsyncSession, user, agent_id: str) -> Agent:
    """对话入口的统一门禁：该 Agent 对当前用户可见（行级）才能聊。

    与列表共用 `agent_read_scope` 的同一套条件（见 core/scope.py），因此
    "列表里看不到"与"打不开详情/发不了消息"天然一致，不会出现
    "隐藏但可直连"的经典漏洞。跨部门私有 Agent → 404（不泄露存在性）。
    """
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    await ensure_agent_access(db, user, a, Capability.VIEW)
    return a


async def _resolve_requested_model(db: AsyncSession, a: Agent,
                                   model_id: str | None) -> str | None:
    """对话框显式选择的模型：必须存在且属于本公司（或未归属的共享配置）。"""
    if not model_id:
        return None
    from app.models.agent import Model
    row = await db.get(Model, model_id)
    if row is None or row.company_id not in (None, str(a.company_id)):
        raise HTTPException(400, "模型不存在或不属于本公司")
    return str(row.id)


# 滚动摘要：攒够 16 条再压一次，最近 6 条始终保留原文。
SUMMARY_TRIGGER_MESSAGES = 16
SUMMARY_KEEP_RECENT = 6
SUMMARY_MAX_CHARS = 800


async def _resolve_thread(db: AsyncSession, a: Agent,
                          thread_id: str | None) -> CeoThread | None:
    """确定本次对话落在哪个线程。

    普通员工私聊保持无线程（thread_id 为空）；CEO 不传线程时复用默认线程，
    首次打开会把历史老消息归入其中，保证"永久保存"从第一天起就成立。
    """
    if not thread_id:
        if not a.is_ceo:
            return None
        return await ensure_default_thread(db, a)

    thread = await db.get(CeoThread, thread_id)
    if (thread is None or str(thread.agent_id) != str(a.id)
            or str(thread.company_id) != str(a.company_id)):
        raise HTTPException(404, "对话不存在")
    return thread


async def _save_msg(db: AsyncSession, agent: Agent, role: str,
                    content: str, meta: dict | None = None,
                    thread: CeoThread | None = None):
    msg = AgentChatMessage(
        id=str(uuid.uuid4()), agent_id=str(agent.id),
        company_id=str(agent.company_id),
        thread_id=str(thread.id) if thread is not None else None,
        role=role, content=content[:8000], meta=meta or {})
    db.add(msg)
    await db.flush()
    if thread is not None:
        thread.message_count = (thread.message_count or 0) + 1
        thread.last_message_at = _now()
        if (role == "boss"
                and (thread.title or DEFAULT_THREAD_TITLE) == DEFAULT_THREAD_TITLE):
            first = (content.strip().splitlines() or [""])[0][:30].strip()
            if first:
                thread.title = first
    return msg


# ---------------- 证据链：把搜索结果变成可点回的来源 ----------------

MAX_CITATIONS = 5
CITATION_SNIPPET_CHARS = 200


def _clean_cite_text(value: object, limit: int) -> str:
    """来源文本统一压成一行再截断，避免把整段网页正文塞进 meta。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _extract_citations(result_data: object) -> list[dict]:
    """从 web_search 的工具结果里提取来源（title / url / snippet）。

    web_search 的结果字段是 ``title/href/body``（部分后端是 ``url``），
    但落到 meta 时统一成前端好渲染的 ``{title, url, snippet}``：
    * 非 dict、没有 results 列表 → 空；
    * 无标题或无链接的条目丢弃（点不回来源的引用没有意义）；
    * snippet 依次取 body / snippet / content，截断 200 字；
    * 按 url 去重，最多 5 条。
    """
    if not isinstance(result_data, dict):
        return []
    results = result_data.get("results")
    if not isinstance(results, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        title = _clean_cite_text(item.get("title"), 200)
        url = str(item.get("url") or item.get("href") or "").strip()
        if not title or not url or url in seen:
            continue
        snippet = ""
        for key in ("body", "snippet", "content"):
            snippet = _clean_cite_text(item.get(key), CITATION_SNIPPET_CHARS)
            if snippet:
                break
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= MAX_CITATIONS:
            break
    return out


def _tool_payload(res: object) -> object:
    """工具返回值可能包一层 {"result": ...}，取出真正的结果体。"""
    if isinstance(res, dict) and isinstance(res.get("result"), dict):
        return res["result"]
    return res


def _merge_citations(collected: list[dict], result_data: object) -> None:
    """把一次工具调用的来源并入总表：按 url 去重，总量封顶 5 条。"""
    if len(collected) >= MAX_CITATIONS:
        return
    seen = {c["url"] for c in collected}
    for cite in _extract_citations(result_data):
        if cite["url"] in seen:
            continue
        seen.add(cite["url"])
        collected.append(cite)
        if len(collected) >= MAX_CITATIONS:
            return


# ---------------- 普通/流式两个入口共用的逻辑 ----------------

DOC_OPEN = "[GENERATE_DOC]"
DOC_CLOSE = "[/GENERATE_DOC]"


def visible_reply(raw: str) -> str:
    """去掉给系统看的文档标记，只留老板该看到的正文。

    标记块是机器指令，落库/展示都不该带上——此前历史消息里会原样出现
    ``[GENERATE_DOC] {"type":...}`` 这一整段 JSON。
    """
    text = re.sub(r"\[GENERATE_DOC\].*?\[/GENERATE_DOC\]", "", raw,
                  flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _agent_model_ids(db: AsyncSession, a: Agent) -> list[str]:
    """按 primary → secondary → fallback 顺序取该员工的模型 id。"""
    from app.models.agent import AgentModelConfig
    cfg_rows = (await db.execute(select(AgentModelConfig).where(
        AgentModelConfig.agent_id == str(a.id)))).scalars().all()
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    return [c.model_id for c in sorted(cfg_rows,
                                       key=lambda c: order.get(c.role, 3))]


async def _skill_prompt(db: AsyncSession, a: Agent, text: str) -> str | None:
    """紧凑技能提示：全量只列一行摘要，方法论只挑本轮最相关的 2 个。

    CEO 绑定 10 张技能卡，全量注入每轮要多花数千 token；而大多数回合
    只用到其中一两项。索引保证"知道自己会什么"，top-2 保证"真会用"。
    """
    from app.knowledge_engine import _tokens
    from app.models.skill import AgentSkill, Skill as SkillRow

    rows = (await db.execute(
        select(SkillRow.slug, SkillRow.name, SkillRow.description,
               SkillRow.input_schema)
        .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
        .where(AgentSkill.agent_id == str(a.id)))).all()
    if not rows:
        return None

    q_tokens = _tokens(text)
    index_lines: list[str] = []
    scored: list[tuple[int, str, str, str]] = []
    for slug, name, desc, schema in rows:
        instruction = ""
        if isinstance(schema, dict):
            instruction = str(schema.get("instruction") or "").strip()
        index_lines.append(f"- {name}（{slug}）：{(desc or '')[:50]}")
        if instruction:
            score = len(q_tokens & _tokens(
                f"{name} {desc or ''} {instruction[:600]}"))
            if score:
                scored.append((score, name, slug, instruction))

    scored.sort(key=lambda x: -x[0])
    out = "你的绑定技能（按需调用）：\n" + "\n".join(index_lines)
    if scored:
        methods = "\n\n".join(
            f"### {name}（{slug}）\n{instruction[:700]}"
            for _score, name, slug, instruction in scored[:2])
        out += "\n\n本轮重点方法论：\n" + methods
    return out[:2600]


_COMPLEX_HINTS = ("方案", "分析", "拆解", "规划", "复盘", "评估", "对比",
                  "策略", "预算", "风险", "招聘", "组织", "报告", "为什么", "如何")


def _model_ids_for_text(model_ids: list[str], text: str) -> list[str]:
    """短问题先用便宜的备用模型，复杂问题仍用主模型。

    网关按顺序尝试、失败才降级，因此"便宜在前"不会降低可用性：
    便宜的挂了照样落到主模型；复杂请求则一开始就走主模型保质量。
    """
    if len(model_ids) < 2:
        return model_ids
    is_complex = len(text) >= 120 or any(k in text for k in _COMPLEX_HINTS)
    if is_complex:
        return model_ids
    return [*model_ids[1:], model_ids[0]]


async def _build_qa_messages(db: AsyncSession, a: Agent, text: str,
                             thread: CeoThread | None = None,
                             attachments_text: str | None = None) -> list[dict]:
    """问答上下文的完整拼装：人设 + 摘要 + 长期记忆 + 知识库 + 技能 + 最近对话。"""
    from app.kb_engine import cost_optimized_rag
    from app.memory_engine import recall

    messages = []
    from datetime import datetime
    now = datetime.now()
    base_prompt = (
        f"你是「{a.name}」，公司的一名数字员工。"
        f"职责：{a.role or '综合事务'}。"
        f"现在老板正在和你单独对话（当前时间：{now:%Y-%m-%d %H:%M}，"
        f"星期{'一二三四五六日'[now.weekday()]}）。回答要：\n"
        "1. 基于你的记忆和知识库里的真实信息；不知道就说不知道，不要编造\n"
        "2. 如果被问到经营数据而你记得相关事实就汇报，没有数据就说明需要核实\n"
        "3. 简洁、口语化，像向老板汇报一样")
    messages.append({"role": "system", "content": base_prompt})

    # 线程摘要：更早的对话已经压缩，避免把整段历史反复塞进上下文
    if thread is not None and thread.summary:
        messages.append({"role": "system",
                         "content": f"此前对话摘要：\n{thread.summary[:1200]}"})

    # 长期记忆召回
    try:
        mems = await recall(db, str(a.id), query=text[:200])
        if mems:
            lines = "\n".join(f"- {m.get('content', '')[:180]}" for m in mems[:6])
            messages.append({"role": "system",
                             "content": f"你的长期记忆：\n{lines}"})
    except Exception as mem_err:
        logger.warning("聊天记忆召回失败 agent=%s: %s", a.id, mem_err, exc_info=True)

    # 知识库（成本优化两级检索）
    try:
        rag = await cost_optimized_rag(db, str(a.company_id), str(a.id),
                                       text[:300], agent=a)
        if rag.get("context_text"):
            messages.append({"role": "system",
                             "content": f"相关知识库内容：\n{rag['context_text']}"})
    except Exception as kb_err:
        logger.warning("聊天知识库检索失败 agent=%s: %s", a.id, kb_err, exc_info=True)

    # 技能：紧凑索引 + 本轮最相关的方法论
    try:
        skill_text = await _skill_prompt(db, a, text)
        if skill_text:
            messages.append({"role": "system", "content": skill_text})
    except Exception as skill_err:
        logger.warning("聊天技能注入失败 agent=%s: %s", a.id, skill_err,
                       exc_info=True)

    # 最近对话上下文（最近7条，跳过刚存的当前消息避免重复）
    q_recent = select(AgentChatMessage).where(
        AgentChatMessage.agent_id == str(a.id))
    if thread is not None:
        q_recent = q_recent.where(AgentChatMessage.thread_id == str(thread.id))
    recent = (await db.execute(q_recent.order_by(
        AgentChatMessage.created_at.desc()).limit(7))).scalars().all()
    for r in reversed(recent[1:]):  # [0] 是刚存的当前 boss 消息，跳过
        messages.append({"role": "user" if r.role == "boss" else "assistant",
                         "content": r.content[:400]})

    # 输出长度指引 + 文档产出能力
    messages.append({"role": "system", "content":
        "回答长度要求：默认完整充分（300-800字），用户明确要求简短时才简短。"
        "生成文档类任务按文档需要输出完整内容，不要偷工减料。"})
    messages.append({"role": "system", "content":
        "【文档产出能力】当老板要求你生成 Word/PPT/Excel/MD 文件时，"
        "在回复末尾按以下格式输出（系统会自动渲染为真实文件）：\n"
        "[GENERATE_DOC]\n"
        '{"type":"word|ppt|excel|md","title":"文档标题",'
        '"content":"完整内容，ppt用##分页"}\n'
        "[/GENERATE_DOC]\n"
        "type 对应：word=Word, ppt=PPT, excel=Excel, md=Markdown。"})
    # 工具使用纪律：联网能力要在"该查的时候真的去查"，而不是凭记忆编或直接说不知道
    from app.tools import tool_system_prompt
    messages.append({"role": "system", "content": tool_system_prompt()})
    # 实时信息强制搜索规则
    realtime_kw = any(k in text for k in ("今天","现在","最新","目前","当前","实时","汇率","天气","温度","新闻","趋势","价格","多少度","多少钱"))
    if realtime_kw:
        messages.append({"role": "system", "content":
            "【强制规则】用户的问题涉及实时/最新信息，你不知道答案。"
            "先调用 web_search 获取真实数据；摘要里没有具体数字/温度时，"
            "必须用 web_fetch 抓取结果中最权威的链接正文"
            "（天气用 weather.com.cn / nmc.cn），再基于正文作答。"
            "最多搜索 2 次，不要反复换关键词搜索；"
            "搜索/抓取失败要如实说明，严禁凭记忆或猜测给出数字。"})
    # 本轮附件：放在用户消息前，模型能明确知道"这些资料是这次带来的"
    if attachments_text:
        messages.append({"role": "system", "content": attachments_text})
    # 当前用户消息（必须显式加入）
    messages.append({"role": "user", "content": text})
    return messages


async def _try_short_intents(db: AsyncSession, a: Agent, user_id: str,
                             text: str) -> dict | None:
    """不需要 LLM 的两条短路径：本机操作、安排任务。

    返回 ``{"reply", "actions", "meta"}``；不匹配返回 None（走问答）。
    普通与流式入口共用，避免两套意图识别各自漂移。
    """
    from app.nl_computer import detect_intent
    nl_intent = detect_intent(text)
    if nl_intent:
        from app.nl_computer import execute_nl
        result = await execute_nl(nl_intent, company_id=str(a.company_id),
                                  agent_id=str(a.id))
        reply_text = f"{result.get('message') or result.get('error') or '操作完成'}"
        detail = result.get("detail")
        if detail:
            lines = [f"• {m['file']} → {m['category']}/" for m in detail[:8]]
            if len(detail) >= 8:
                lines.append("…")
            reply_text += "\n" + "\n".join(lines)
        return {"reply": reply_text,
                "actions": [{"type": "nl_action", **nl_intent, "result": result}],
                "meta": {"action": nl_intent["action"]}}

    create_task_intent = any(kw in text for kw in
                             ("安排任务", "下达任务", "去干", "去做", "办一件事",
                              "执行一个任务", "安排一下", "负责处理"))
    if not create_task_intent:
        return None

    # 找消息里提到的其他 Agent 名（排除自己）
    target_agent = None
    others = (await db.execute(select(Agent).where(
        Agent.company_id == a.company_id))).scalars().all()
    for o in others:
        if o.id == a.id:
            continue
        # 名称全匹配 或 老板用称呼片段（如"文案"匹配"文案写手"）
        name_hit = o.name in text
        role_hit = bool(o.role) and len(o.role) >= 2 and o.role in text
        prefix_hit = len(o.name) >= 2 and o.name[:2] in text
        if name_hit or role_hit or prefix_hit:
            target_agent = o
            break

    from app.models.task import Task as TaskModel, TaskStatus
    title = text[:80]
    assignee = target_agent or a
    t = TaskModel(company_id=a.company_id, title=title,
                  description=text, assigned_agent_id=assignee.id,
                  created_by=str(user_id), status=TaskStatus.TODO)
    db.add(t)
    await db.flush()
    task_created = {"task_id": str(t.id), "assignee": assignee.name}
    reply_text = (
        f"✅ 已收到工作安排，我已创建任务《{title[:50]}》"
        + (f"并指派给「{target_agent.name}」执行。" if target_agent
           else "，稍后开始执行。"))
    return {"reply": reply_text,
            "actions": [{"type": "create_task", **task_created}],
            "meta": {"action": "create_task", **task_created}}


async def _materialize_docs(db: AsyncSession, a: Agent, raw_reply: str):
    """把回复里的 [GENERATE_DOC] 标记渲染成真实文件并登记成果。

    返回 ``(file, error)``：失败不阻断对话，但必须把原因带回给老板
    ——此前这类失败被静默吞掉，用户"看到回复却拿不到文件"。
    """
    doc_file = None
    doc_error = None
    try:
        from app.doc_generator import parse_doc_markers, generate_document
        from app.models.asset import Asset as _Asset, AssetType
        for dspec in parse_doc_markers(raw_reply):
            gen = generate_document(dspec["type"], dspec["title"],
                                    dspec["content"], agent_id=str(a.id))
            db.add(_Asset(company_id=a.company_id,
                          name=gen["file_name"],
                          type=AssetType.OTHER,
                          path=gen["file_path"], mime_type=gen["mime"],
                          size=gen["size"], created_by=str(a.id),
                          creator_type="agent", agent_id=str(a.id)))
            doc_file = gen
    except Exception as doc_err:
        logger.warning("聊天内文档生成失败 agent=%s: %s", a.id, doc_err,
                       exc_info=True)
        doc_error = f"{type(doc_err).__name__}: {doc_err}"
    return doc_file, doc_error


async def _maybe_summarize_thread(db: AsyncSession, a: Agent,
                                  thread: CeoThread | None,
                                  gw, model_ids: list[str]) -> None:
    """滚动摘要：攒够一批旧消息就压缩，并抽取少量长期记忆。

    只动"最近 6 条之外"的消息：近期原文始终留在上下文里，摘要负责
    更早的事实与承诺。摘要失败只记日志，绝不阻断对话——原始消息还在。
    """
    if thread is None or not model_ids:
        return
    done = int(thread.summarized_count or 0)
    total = int(thread.message_count or 0)
    if total - done < SUMMARY_TRIGGER_MESSAGES:
        return
    take = total - done - SUMMARY_KEEP_RECENT
    if take <= 0:
        return

    rows = (await db.execute(
        select(AgentChatMessage)
        .where(AgentChatMessage.thread_id == str(thread.id))
        .order_by(AgentChatMessage.created_at).offset(done).limit(take)
    )).scalars().all()
    if not rows:
        return

    convo = "\n".join(
        f"{'老板' if r.role == 'boss' else 'CEO'}：{r.content[:260]}"
        for r in rows)
    prompt = (
        "把「已有纪要」和「新增对话」合并成一份不超过 500 字的滚动纪要，"
        "只保留事实、决定、承诺、待办和老板偏好，去掉寒暄与重复。"
        "第一行直接给纪要正文，不要标题。"
        "然后另起若干行，用 `MEM: ` 开头列出最多 3 条值得长期记住的事实；"
        "没有就写 `MEM: 无`。")
    try:
        res = await gw.chat_with_agent_models(
            [model_ids[-1]],
            [{"role": "system", "content": prompt},
             {"role": "user", "content":
              f"已有纪要：\n{thread.summary or '（无）'}\n\n新增对话：\n{convo}"}])
    except Exception as summary_err:
        logger.warning("线程摘要生成失败 agent=%s thread=%s: %s",
                       a.id, thread.id, summary_err, exc_info=True)
        return

    raw = (res.get("content") or "").strip()
    if not raw:
        return
    summary_lines: list[str] = []
    facts: list[str] = []
    for line in raw.splitlines():
        item = line.strip()
        if item.upper().startswith("MEM:"):
            fact = item[4:].strip()
            if fact and fact != "无":
                facts.append(fact)
        elif item:
            summary_lines.append(item)
    summary = "\n".join(summary_lines).strip()
    if summary:
        thread.summary = summary[:SUMMARY_MAX_CHARS]
    thread.summarized_count = done + len(rows)

    if facts:
        from app.memory_engine import MemoryType, remember
        for fact in facts[:3]:
            try:
                await remember(db, str(a.id), str(a.company_id),
                               MemoryType.SEMANTIC, fact[:400],
                               importance=0.75)
            except Exception as mem_err:
                logger.warning("对话长期记忆写入失败 agent=%s: %s",
                               a.id, mem_err, exc_info=True)


@router.get("/{aid}/chat/history")
async def chat_history(aid: str, limit: int = Query(50, ge=1, le=200),
                       thread_id: str | None = Query(None),
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    a = await _visible_agent(db, user, aid)
    q = select(AgentChatMessage).where(AgentChatMessage.agent_id == str(a.id))
    if thread_id:
        thread = await _resolve_thread(db, a, thread_id)
        q = q.where(AgentChatMessage.thread_id == str(thread.id))
    rows = (await db.execute(q.order_by(
        AgentChatMessage.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "role": r.role, "content": r.content,
             "meta": r.meta, "thread_id": r.thread_id,
             "created_at": str(r.created_at)}
            for r in reversed(rows)]


@router.post("/{aid}/chat")
async def boss_chat(aid: str, data: ChatIn,
                    db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """老板与 Agent 单独聊天：问答 / 下达任务 / 指挥其他 Agent。"""
    from app.model_gateway import ModelGateway
    from app.memory_engine import MemoryType, remember

    a = await _visible_agent(db, user, aid)
    text = data.message.strip()
    if not text:
        raise HTTPException(400, "empty message")
    thread = await _resolve_thread(db, a, data.thread_id)

    # 本次对话触发的动作汇总（前端用来展示"我到底做了什么"）。
    # 提到函数开头显式初始化：原代码用 locals().get("_actions") 做惰性判断，
    # 依赖 CPython 局部变量字典的行为，既脆弱又难读。
    actions: list[dict] = []
    # 证据链：本轮 web_search 命中的来源，随回复一起落库并回传前端
    citations: list[dict] = []

    await _save_msg(db, a, "boss", text, thread=thread)

    # 本机操作 / 安排任务：不需要 LLM，两步可秒回
    short = await _try_short_intents(db, a, str(user.id), text)
    if short:
        await _save_msg(db, a, "agent", short["reply"], short["meta"],
                        thread=thread)
        await db.commit()
        return {"reply": short["reply"], "actions": short["actions"],
                "citations": []}

    # ---------- 普通问答：记忆 + 知识库 + LLM ----------
    messages = await _build_qa_messages(db, a, text, thread=thread)

    gw = ModelGateway(db)
    from app.model_gateway import usage_context
    # 后续 150+ 行含多次 LLM 调用，with 无法包裹，显式 enter/exit：
    # 函数末尾 commit 前 exit；异常上抛路径沿用旧语义（不比原来差）。
    _usage_scope = usage_context(agent=a, scenario="CHAT", user_id=str(user.id))
    _usage_scope.__enter__()
    model_ids = _model_ids_for_text(await _agent_model_ids(db, a), text)
    llm = await gw.chat_with_agent_models(model_ids, messages, tools=_CHAT_TOOLS)
    # 处理 tool_calls：调用 web_search 后回填消息再循环
    tool_calls = llm.get("tool_calls") or []
    final_reply = (llm.get("content") or "").strip()
    # agnes 等模型可能以文本形式输出 <tool_call>（无论是否有结构化字段），一律解析并剥离
    text_calls = _parse_text_tool_calls(final_reply)
    if text_calls:
        for tc in text_calls:
            if not any((t.get("function") or {}).get("arguments") == tc["function"]["arguments"]
                       for t in tool_calls):
                tool_calls.append(tc)
        final_reply = re.sub(
            r"<tool_call>\s*<function=\w+>\s*.*?</function>\s*</tool_call>",
            "", final_reply, flags=re.S).strip()
    if tool_calls:
        from app.skill_system import execute_skill

        # 与流式入口同理：web_fetch 用独立会话写审计日志，先把本会话的
        # 待提交写入（含刚产生的用量记录）落盘，避免 SQLite 写锁互等。
        await db.commit()
        tool_results = []
        search_summary = []
        for tc in tool_calls:
            fname = tc.get("function", {}).get("name", "")
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                if not isinstance(args, dict):
                    args = {}
                is_computer = fname.startswith("computer_")
                if is_computer:
                    cap = fname.replace("computer_", "")
                    res = await _computer_action(db, cap, args, user.id, a.company_id)
                else:
                    res = await execute_skill_safe(
                        db, fname, args, company_id=str(a.company_id),
                        agent_id=str(a.id))
                result_data = res.get("result") or res
                # 收集 computer 操作，供前端展示"我做了什么"
                if is_computer:
                    actions.append({
                        "type": "computer_action",
                        "action": fname.replace("computer_", ""),
                        "command": args.get("command", ""),
                        "result": res})
                # 修复：原代码在同一个字典里写了两次 tool_call_id（后者覆盖前者），
                # 属于复制粘贴残留；此处去重。
                tool_results.append({"tool_call_id": tc.get("id"), "role": "tool",
                                     "content": str(result_data)})
                if fname == "web_search" and isinstance(result_data, dict):
                    hits = result_data.get("results") or []
                    search_summary = [f"• {h.get('title','')}: {h.get('body','')[:120]}"
                                      for h in hits[:5] if h.get('title')]
                    _merge_citations(citations, result_data)
            except Exception as e:
                tool_results.append({"tool_call_id": tc.get("id"), "role": "tool",
                                     "tool_call_id": tc.get("id"), "content": f"error: {e}"})
        messages.append({"role": "assistant", "content": final_reply,
                         "tool_calls": tool_calls})
        for tr in tool_results:
            messages.append(tr)
        second = await gw.chat_with_agent_models(model_ids, messages, tools=_CHAT_TOOLS)
        second_reply = (second.get("content") or "").strip()
        # second 可能又输出文本形式 tool_call 或空——循环处理（最多3轮）
        rounds = 0
        while rounds < 3:
            s_calls = _parse_text_tool_calls(second_reply)
            s_struct = second.get("tool_calls") or []
            if s_struct:
                # 模型还想继续调用工具：执行并继续
                for tc in s_struct:
                    try:
                        args2 = json.loads((tc.get("function") or {}).get("arguments", "{}"))
                        res2 = await execute_skill_safe(
                            db, tc["function"]["name"], args2,
                            company_id=str(a.company_id), agent_id=str(a.id))
                        if tc["function"]["name"] == "web_search":
                            _merge_citations(citations, _tool_payload(res2))
                        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                         "content": str(res2)})
                    except Exception as tool_err:
                        # 原为 break：静默中断工具链，且不把失败告知模型，
                        # 模型会在"工具没返回"的状态下继续，产生幻觉式回复
                        logger.warning("续轮工具执行失败 tool=%s agent=%s: %s",
                                       (tc.get("function") or {}).get("name"), a.id,
                                       tool_err, exc_info=True)
                        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                         "content": f"error: {tool_err}"})
                second = await gw.chat_with_agent_models(model_ids, messages, tools=_CHAT_TOOLS)
                second_reply = (second.get("content") or "").strip()
                rounds += 1
                continue
            if s_calls and not s_struct:
                stripped = re.sub(r"<tool_call>\s*<function=\w+>\s*.*?</function>\s*</tool_call>",
                                  "", second_reply, flags=re.S).strip()
                for tc in s_calls:
                    try:
                        args2 = json.loads(tc["function"]["arguments"])
                        res2 = await execute_skill_safe(
                            db, tc["function"]["name"], args2,
                            company_id=str(a.company_id), agent_id=str(a.id))
                        if tc["function"]["name"] == "web_search":
                            _merge_citations(citations, _tool_payload(res2))
                        messages.append({"role": "user",
                                         "content": f"[搜索结果] {str(res2)[:1500]}"})
                    except Exception as tool_err:
                        logger.warning("二次工具调用失败 tool=%s agent=%s: %s",
                                       tc.get("function", {}).get("name"), a.id,
                                       tool_err, exc_info=True)
                second = await gw.chat_with_agent_models(model_ids, messages)
                second_reply = (second.get("content") or "").strip()
                if "<tool_call>" not in second_reply:
                    break
                rounds += 1
                continue
            break
        if second_reply and "<tool_call>" not in second_reply:
            final_reply = second_reply
        elif search_summary:
            final_reply = "根据搜索结果总结：\n" + "\n".join(search_summary)
        else:
            final_reply = (re.sub(r"<tool_call>.*?</tool_call>", "",
                                  final_reply, flags=re.S).strip() or "(无回复)")

    # 聊天中检测文档生成标记 → 自动产出真实文件
    doc_file, doc_error = await _materialize_docs(db, a, final_reply)

    # 落库与展示都只留正文：标记块是给系统看的
    final_reply = visible_reply(final_reply) or "(无回复)"
    # 证据链随回复落库，/chat/history 的 meta 里即可回放同一份来源
    await _save_msg(db, a, "agent", final_reply,
                    {"citations": citations} if citations else None,
                    thread=thread)

    # 省 token 记忆：普通私聊每 6 轮压一条摘要；CEO 线程走滚动摘要（更省）
    if thread is None:
        try:
            cnt = (await db.execute(select(AgentChatMessage).where(
                AgentChatMessage.agent_id == str(a.id)))).scalars().all()
            if len(cnt) >= 12 and len(cnt) % 6 == 0:
                recent_txt = "\n".join(
                    f"{'老板' if m.role == 'boss' else '我'}: {m.content[:120]}"
                    for m in cnt[-6:])
                digest = await gw.chat_with_agent_models(model_ids, [
                    {"role": "system", "content":
                        "把这6条对话压缩成一条不超过100字的记忆要点，"
                        "只保留关键事实、决定和承诺，不要客套话。只输出要点。"},
                    {"role": "user", "content": recent_txt},
                ])
                from app.memory_engine import MemoryType, remember
                await remember(db, str(a.id), str(a.company_id),
                               MemoryType.EPISODIC,
                               (digest.get("content") or "").strip()[:300],
                               importance=0.7)
        except Exception as digest_err:
            # 记忆压缩失败不阻断对话，但会导致记忆越攒越多直至超出上下文预算
            logger.warning("对话记忆压缩失败 agent=%s: %s", a.id, digest_err,
                           exc_info=True)

    try:
        await _maybe_summarize_thread(db, a, thread, gw, model_ids)
    except Exception as summary_err:
        logger.warning("线程滚动摘要失败 agent=%s: %s", a.id, summary_err,
                       exc_info=True)

    _usage_scope.__exit__(None, None, None)
    await db.commit()
    return {"reply": final_reply, "file": doc_file, "doc_error": doc_error,
            "actions": actions, "citations": citations,
            "thread_id": str(thread.id) if thread is not None else None}


# ---------------- 流式聊天（SSE） ----------------

_CHAT_TOOLS = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL, *COMPUTER_TOOLS]


class ChatAttachmentIn(BaseModel):
    id: str
    name: str | None = None


class ChatStreamIn(BaseModel):
    message: str
    thread_id: str | None = None
    # 老板在对话框里选的附件（先走 /assets/upload，这里只传资产 id）
    attachments: list[ChatAttachmentIn] = Field(default_factory=list)
    # 老板在对话框里显式选的模型；缺省仍走 Agent 的 primary/降级顺序
    model_id: str | None = None


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _marker_holdback_len(s: str) -> int:
    """s 末尾有多少字符可能是 [GENERATE_DOC] 的开头（需要暂缓发送）。"""
    for n in range(min(len(s), len(DOC_OPEN) - 1), 0, -1):
        if DOC_OPEN.startswith(s[-n:]):
            return n
    return 0


class DocMarkerFilter:
    """流式过滤器：把 [GENERATE_DOC]…[/GENERATE_DOC] 整段挡在气泡之外。

    标记是给系统看的指令，不该出现在对话里；而它是逐字到达的，
    所以只有"能确定不是标记前缀"的尾巴才放行——否则老板会先看到
    ``[GENER`` 这样的碎片。
    """

    def __init__(self) -> None:
        self.raw = ""       # 原文（含标记），最后交给文档生成
        self._buf = ""
        self._in_doc = False

    def feed(self, piece: str) -> str:
        self.raw += piece
        self._buf += piece
        if self._in_doc:
            end = self._buf.find(DOC_CLOSE)
            if end < 0:
                return ""
            self._in_doc = False
            rest = self._buf[end + len(DOC_CLOSE):]
            self._buf = ""
            return rest
        start = self._buf.find(DOC_OPEN)
        if start >= 0:
            emit = self._buf[:start]
            self._buf = self._buf[start:]
            self._in_doc = True
            return emit
        keep = _marker_holdback_len(self._buf)
        if not keep:
            emit, self._buf = self._buf, ""
            return emit
        emit, self._buf = self._buf[:-keep], self._buf[-keep:]
        return emit

    def flush(self) -> str:
        if self._in_doc:
            return ""   # 标记没闭合：半截指令没有意义，整段丢弃
        out, self._buf = self._buf, ""
        return out


# ---------------- 过程可视化：阶段 / 模型 / 工具 / 思考 ----------------

MAX_PROCESS_STEPS = 30          # 落库的过程步骤上限
MAX_PROCESS_REASONING = 4000    # 落库的思考正文上限（实时流不截断）


def _tool_label(fname: str) -> str:
    """工具名 → 老板看得懂的一句话，别把 snake_case 直接甩到界面上。"""
    if fname == "web_search":
        return "联网搜索"
    if fname == "web_fetch":
        return "抓取网页正文"
    if fname.startswith("computer_"):
        return f"操作电脑 · {fname.replace('computer_', '')}"
    return fname or "调用工具"


def _tool_args_preview(fname: str, targs: dict) -> str:
    """只挑能说明意图的参数，避免整页 JSON 塞进时间线。"""
    for key in ("query", "url", "command", "path", "filename", "title"):
        value = targs.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    if not targs:
        return ""
    try:
        return json.dumps(targs, ensure_ascii=False)[:160]
    except (TypeError, ValueError):
        return str(targs)[:160]


def _tool_summary(fname: str, payload: object, body: str,
                  ok: bool, error: str = "") -> str:
    """工具结果压成一行：条数/字数比整段结果更有信息量。"""
    if not ok:
        return f"失败：{error or '未知错误'}"[:220]
    data = payload if isinstance(payload, dict) else {}
    if fname == "web_search":
        results = data.get("results")
        if isinstance(results, list):
            return f"找到 {len(results)} 条来源"
    if fname == "web_fetch":
        for key in ("text", "content", "body"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return f"已抓取正文（{len(value)} 字）"
    if fname.startswith("computer_"):
        return f"已执行 {fname.replace('computer_', '')}"
    text = " ".join(str(body or "").split())
    return (text[:180] + "…") if len(text) > 180 else (text or "已返回")


def _process_meta(state: dict, started_at: float) -> dict:
    """把运行期的过程收成可落库的精简版（历史里能回放）。"""
    steps: list[dict] = []
    for s in state["steps"]:
        if len(steps) >= MAX_PROCESS_STEPS:
            break
        if s.get("status") == "running":
            continue
        item = {"kind": s["kind"], "label": s["label"][:80],
                "status": s["status"]}
        if s.get("detail"):
            item["detail"] = s["detail"][:240]
        if s.get("duration_ms") is not None:
            item["duration_ms"] = int(s["duration_ms"])
        steps.append(item)
    out: dict = {"duration_ms": int((time.monotonic() - started_at) * 1000)}
    if steps:
        out["steps"] = steps
    if state["reasoning"]:
        out["reasoning"] = state["reasoning"][:MAX_PROCESS_REASONING]
    if state["model"]:
        out["model"] = state["model"]
    return out


async def _chat_stream(agent_id: str, company_id: str, user_id: str, text: str,
                       thread_id: str | None = None,
                       attachments: list[ChatAttachment] | None = None,
                       requested_model_id: str | None = None):
    """SSE 生成器。

    自建 session：请求级依赖的回收时机与长连接无关，
    流还没开始写的时候它可能已经被关掉了。

    事件分两类：
    * 正文：``delta`` / ``done`` / ``error``
    * 过程：``status``（阶段）、``model`` / ``model_error``（选了谁、是否降级）、
      ``reasoning``（推理模型的思考增量）、``tool_start`` / ``tool_end``
    """
    from app.model_gateway import ModelGateway, usage_context

    attachments = attachments or []
    started_at = time.monotonic()
    # 第一帧先于任何 DB/上下文工作：老板发完消息要立刻看到"已收到"，
    # 否则模型排队或建上下文的那几秒里界面完全空白。
    yield _sse({"type": "status", "stage": "accepted",
                "text": "已收到，正在准备…"})

    state: dict = {"steps": [], "reasoning": "", "model": None,
                   "usage": {}, "tool_calls": None}

    def add_step(kind: str, label: str, status: str = "done",
                 detail: str | None = None) -> dict:
        step = {"kind": kind, "label": label[:80], "status": status}
        if detail:
            step["detail"] = detail[:240]
        if len(state["steps"]) < MAX_PROCESS_STEPS:
            state["steps"].append(step)
        return step

    def status_frame(text: str, stage: str | None = None) -> str:
        add_step("stage", text)
        payload = {"type": "status", "text": text}
        if stage:
            payload["stage"] = stage
        return _sse(payload)

    def handle_gateway_event(ev: dict) -> list[str]:
        """网关事件 → 前端帧 + 本地过程记录。"""
        kind = ev.get("type")
        if kind == "reasoning":
            piece = str(ev.get("text") or "")
            if not piece:
                return []
            room = MAX_PROCESS_REASONING - len(state["reasoning"])
            if room > 0:
                state["reasoning"] += piece[:room]
            return [_sse({"type": "reasoning", "text": piece})]
        if kind == "model":
            name = str(ev.get("model_name") or ev.get("model_id") or "模型")
            state["model"] = name
            add_step("model", f"调用模型 · {name}",
                     detail=f"第 {ev.get('attempt') or 1} 顺位")
            return [_sse({"type": "model", "model_id": ev.get("model_id"),
                          "model_name": name,
                          "attempt": ev.get("attempt")})]
        if kind == "model_error":
            name = str(ev.get("model_name") or ev.get("model_id") or "模型")
            message = str(ev.get("message") or "调用失败")
            add_step("model_error", f"{name} 调用失败", "error", message)
            return [_sse({"type": "model_error",
                          "model_id": ev.get("model_id"),
                          "model_name": name, "message": message})]
        if kind == "done":
            state["tool_calls"] = ev.get("tool_calls")
            state["usage"] = ev.get("usage") or {}
            return []
        return []

    async with AsyncSessionLocal() as db:
        try:
            # 鉴权已在 handler 里做完（_visible_agent → 未通过时直接 404）。
            # 这里只处理"生成过程中被删"的竞态，属于数据竞态而不是权限判定。
            a = await db.get(Agent, agent_id)
            if a is None:
                yield _sse({"type": "error", "message": "员工不存在或已离职"})
                return
            thread = await db.get(CeoThread, thread_id) if thread_id else None

            # 带附件时不能走"安排任务"等短路径：那会把文件当没看见
            short = None
            if not attachments:
                yield status_frame("正在理解你的意图…", "intent")
                short = await _try_short_intents(db, a, user_id, text)
            if short:
                for act in short.get("actions") or []:
                    if act.get("type") == "create_task":
                        add_step("action", "已创建任务",
                                 detail=f"指派给 {act.get('assignee') or '本人'}")
                meta = dict(short.get("meta") or {})
                meta["process"] = _process_meta(state, started_at)
                await _save_msg(db, a, "agent", short["reply"], meta,
                                thread=thread)
                await db.commit()
                # 短路径本来就是秒回，也按流式帧发，前端只有一套逻辑
                yield _sse({"type": "delta", "text": short["reply"]})
                yield _sse({"type": "done", "reply": short["reply"],
                            "actions": short["actions"], "citations": [],
                            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                            "thread_id": thread_id})
                return

            attachments_text = ""
            if attachments:
                yield status_frame("正在读取附件…", "attachments")
                attachments_text = await build_attachments_context(attachments)
            yield status_frame("正在检索记忆与知识库…", "context")
            messages = await _build_qa_messages(
                db, a, text, thread=thread, attachments_text=attachments_text)
            model_ids = await _agent_model_ids(db, a)
            if requested_model_id:
                # 老板在对话框里显式选的模型优先，且不参与"短问题走便宜模型"的重排
                model_ids = [requested_model_id] + [
                    m for m in model_ids if m != requested_model_id]
            else:
                model_ids = _model_ids_for_text(model_ids, text)
            gw = ModelGateway(db)
            flt = DocMarkerFilter()
            actions: list[dict] = []
            # 与 done 帧、落库 meta 共用同一份来源（只存 title/url/snippet）
            citations: list[dict] = []

            with usage_context(agent=a, scenario="CHAT", user_id=user_id):
                rounds = 0
                while True:
                    async for ev in gw.stream_with_agent_models(
                            model_ids, messages, tools=_CHAT_TOOLS):
                        if ev.get("type") == "delta":
                            piece = flt.feed(ev["text"])
                            if piece:
                                yield _sse({"type": "delta", "text": piece})
                        else:
                            for frame in handle_gateway_event(ev):
                                yield frame

                    tool_calls = state["tool_calls"]
                    # 搜索 → 抓正文 → 作答是三步，留 3 轮工具预算
                    if not tool_calls or rounds >= 3:
                        break
                    rounds += 1
                    # web_fetch 走独立会话写 browser_action_logs；SQLite 只允许
                    # 一个写事务，先把本会话待提交的用量/消息落盘，否则工具侧
                    # 提交会一直等到 busy_timeout 后报 "database is locked"。
                    await db.commit()
                    yield status_frame(f"正在调用工具（第 {rounds} 轮）…", "tools")
                    messages.append({"role": "assistant", "content": "",
                                     "tool_calls": tool_calls})
                    for tc in tool_calls:
                        fname = (tc.get("function") or {}).get("name", "")
                        targs: dict = {}
                        try:
                            parsed = json.loads(
                                (tc.get("function") or {}).get("arguments") or "{}")
                            if isinstance(parsed, dict):
                                targs = parsed
                        except json.JSONDecodeError as exc:
                            # 参数不是合法 JSON 时工具会拿到空参数执行，留痕便于排查模型输出问题
                            logger.warning("流式工具参数解析失败 tool=%s agent=%s: %s",
                                           fname, agent_id, exc)
                        tool_id = tc.get("id") or f"tool-{uuid.uuid4().hex[:8]}"
                        label = _tool_label(fname)
                        preview = _tool_args_preview(fname, targs)
                        step = add_step("tool", label, "running", preview)
                        tool_started = time.monotonic()
                        yield _sse({"type": "tool_start", "id": tool_id,
                                    "name": fname, "label": label,
                                    "args": preview})
                        ok = True
                        error_text = ""
                        payload: object = None
                        try:
                            if fname.startswith("computer_"):
                                cap = fname.replace("computer_", "")
                                res = await _computer_action(
                                    db, cap, targs, user_id, company_id)
                                actions.append({"type": "computer_action",
                                                "action": cap,
                                                "command": targs.get("command", ""),
                                                "result": res})
                            else:
                                res = await execute_skill_safe(
                                    db, fname, targs, company_id=company_id,
                                    agent_id=agent_id)
                                payload = _tool_payload(res)
                                if fname == "web_search":
                                    _merge_citations(citations, payload)
                            body = str((res.get("result")
                                        if isinstance(res, dict) else None) or res)
                        except Exception as tool_err:
                            # 工具失败要让模型知道，否则它会基于"工具没返回"编答案
                            ok = False
                            error_text = str(tool_err) or type(tool_err).__name__
                            logger.warning("流式聊天工具失败 tool=%s agent=%s: %s",
                                           fname, agent_id, tool_err, exc_info=True)
                            body = f"error: {tool_err}"
                        duration_ms = int((time.monotonic() - tool_started) * 1000)
                        summary = _tool_summary(
                            fname, payload, body, ok, error_text)
                        step["status"] = "done" if ok else "error"
                        step["duration_ms"] = duration_ms
                        step["detail"] = (
                            f"{preview} · {summary}" if preview else summary)[:240]
                        yield _sse({"type": "tool_end", "id": tool_id,
                                    "name": fname, "ok": ok,
                                    "summary": summary,
                                    "duration_ms": duration_ms})
                        messages.append({"role": "tool",
                                         "tool_call_id": tc.get("id"),
                                         "content": body})
                    state["tool_calls"] = None

                # 工具轮次用尽仍未吐正文：去掉 tools 再问一次，逼模型基于
                # 已拿到的搜索结果作答。否则前端只会收到一堆 status 和空回复。
                if not flt.raw.strip():
                    await db.commit()
                    yield status_frame("正在整理搜索结果…", "finalize")
                    messages.append({"role": "system", "content":
                        "现在必须直接回答老板的问题：基于上面的工具结果作答；"
                        "数据缺失就如实说明缺哪一项，不要再调用工具。"})
                    async for ev in gw.stream_with_agent_models(model_ids, messages):
                        if ev.get("type") == "delta":
                            piece = flt.feed(ev["text"])
                            if piece:
                                yield _sse({"type": "delta", "text": piece})
                        else:
                            for frame in handle_gateway_event(ev):
                                yield frame

            tail = flt.flush()
            if tail:
                yield _sse({"type": "delta", "text": tail})
            doc_file, doc_error = await _materialize_docs(db, a, flt.raw)
            reply = visible_reply(flt.raw) or "（这次没有生成内容，请再说一次或换个问法）"
            meta: dict = {}
            if citations:
                meta["citations"] = citations
            # 过程随消息落库：重新打开历史也能看到当时思考/调用了什么
            meta["process"] = _process_meta(state, started_at)
            await _save_msg(db, a, "agent", reply, meta, thread=thread)
            try:
                await _maybe_summarize_thread(db, a, thread, gw, model_ids)
            except Exception as summary_err:
                logger.warning("流式线程摘要失败 agent=%s: %s", agent_id,
                               summary_err, exc_info=True)
            await db.commit()
            yield _sse({"type": "done", "reply": reply, "file": doc_file,
                        "doc_error": doc_error, "actions": actions,
                        "citations": citations,
                        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                        "model": state["model"],
                        "usage": state["usage"],
                        "thread_id": str(thread.id) if thread is not None else None})
        except Exception as exc:
            logger.warning("流式聊天失败 agent=%s: %s", agent_id, exc, exc_info=True)
            try:
                await db.rollback()
            except Exception:
                logger.warning("流式聊天回滚失败 agent=%s", agent_id, exc_info=True)
            yield _sse({"type": "error",
                        "message": str(exc) or type(exc).__name__,
                        "elapsed_ms": int((time.monotonic() - started_at) * 1000)})


@router.post("/{aid}/chat/stream")
async def boss_chat_stream(aid: str, data: ChatStreamIn,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """老板与 Agent 私聊（流式）。

    与 ``/chat`` 共用意图识别、上下文构建和文档产出，差别只在"边生成边推"。
    事件类型：
    * ``status`` 阶段提示（准备/检索/调工具），带 ``stage`` 便于前端归组
    * ``model`` / ``model_error`` 正在用哪个模型、失败后是否降级
    * ``reasoning`` 推理模型的思考增量（没开思考的模型不会发）
    * ``tool_start`` / ``tool_end`` 工具调用的开始、结果与耗时
    * ``delta``  正文增量，前端直接追加
    * ``done``   结束，带完整 reply / file / actions / elapsed_ms
    * ``error``  失败原因（本轮正文不会落库）
    """
    # 可见性校验必须在返回 StreamingResponse **之前**：一旦开流，
    # 响应码已经发出，越权只能被降级成 SSE 里的 error 文本——那等于把
    # 404 吞掉（前端会当成"模型报错"而不是"你没有这个员工"）。
    a = await _visible_agent(db, user, aid)
    text = data.message.strip()
    if not text:
        raise HTTPException(400, "empty message")
    thread = await _resolve_thread(db, a, data.thread_id)
    requested_model = await _resolve_requested_model(db, a, data.model_id)
    attachments = await resolve_attachments(
        db, str(a.company_id), [x.model_dump() for x in data.attachments])
    # 先落老板这句：即使后面模型挂了，历史里也留有他的指令与附件名
    await _save_msg(
        db, a, "boss", text,
        meta={"attachments": [x.meta() for x in attachments]}
        if attachments else None,
        thread=thread)
    await db.commit()

    return StreamingResponse(
        _chat_stream(str(a.id), str(a.company_id), str(user.id), text,
                     str(thread.id) if thread is not None else None,
                     attachments=attachments,
                     requested_model_id=requested_model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------- Agent 删除时级联清空聊天 ----------------
from sqlalchemy import event  # noqa: E402


@event.listens_for(Agent, "before_delete")
def _clear_chat(mapper, connection, target):
    connection.execute(AgentChatMessage.__table__.delete().where(
        AgentChatMessage.__table__.c.agent_id == str(target.id)))
    connection.execute(CeoThread.__table__.delete().where(
        CeoThread.__table__.c.agent_id == str(target.id)))


class GenDocIn(BaseModel):
    doc_type: str          # word | excel | ppt | md
    title: str
    instruction: str       # 生成要求（LLM 据此产出内容）


@router.post("/{aid}/chat/generate-doc")
async def chat_generate_doc(aid: str, data: GenDocIn,
                            db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    """聊天中让 Agent 直接生成 PPT/Word/Excel/MD 文件。
    LLM 根据指令生成结构化内容 → doc_generator 渲染为真实文档
    → 存入该 Agent 专属交付文件夹 → 返回下载信息。"""
    from app.doc_generator import generate_document
    from app.model_gateway import ModelGateway

    a = await _visible_agent(db, user, aid)

    type_names = {"word": "Word 文档", "excel": "Excel 表格",
                  "ppt": "PPT 演示文稿", "md": "Markdown 文档"}
    tname = type_names.get(data.doc_type.lower())
    if not tname:
        raise HTTPException(400, f"doc_type 必须是 word/excel/ppt/md")

    # ---- 先建立消息骨架，再注入风格提示 ----
    #
    # 改造说明：原代码把风格/公文提示 `messages.append(...)` 写在
    # `messages = [...]` 之前，且引用了未定义的 `agent`（本函数里变量名是 `a`）。
    # 两个 NameError 被外层 `except Exception: pass` 吞掉，
    # 于是「文档风格 + 公文写作」这块能力 100% 静默失效——
    # 接口返回 200、文档也生成了，但风格/文种要求从未生效。
    # 现在把顺序理顺，并让失败可见。

    # 结构提示：ppt 按 ## 分页；word 按 #/##/- 层级
    struct_hint = {
        "ppt": "用 '## 页面标题' 分页，每页下用 '- 要点' 列内容，共5-8页。",
        "word": "用 '# 一级标题'、'## 二级标题' 组织章节，'- ' 为要点列表。",
        "md": "标准 Markdown 格式，含标题层级和列表。",
        "excel": "输出 markdown 表格（|列|列|格式），或 JSON 数组。",
    }.get(data.doc_type.lower(), "")

    messages: list[dict] = [
        {"role": "system", "content":
            f"你是「{a.name}」，{a.role or '数字员工'}。"
            f"老板要求你生成一份{tname}。{struct_hint}\n"
            "只输出生成的文档正文内容，不要解释。"},
    ]

    # P4: 文档风格 + 公文写作能力注入（按指令关键词识别文种）
    try:
        from app.models.skill import AgentSkill, Skill as SkillRow
        from app.public_doc_skill import (
            get_bound_style,
            get_public_doc_system_prompt,
            resolve_style,
        )

        skill_names = (await db.execute(
            select(SkillRow.name)
            .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
            .where(AgentSkill.agent_id == str(a.id)))).scalars().all()
        custom = await get_bound_style(db, a)
        style = resolve_style(a.role, list(skill_names), custom_style=custom)
        messages.append({"role": "system", "content":
            f"【文档风格要求】使用「{style['style_name']}"
            + ("" if style["is_custom"] else "（该员工未配置专属风格技能，采用通用格式）")
            + "」：" + style["hint"]})

        instr = (data.instruction or "").lower()
        if any(kw in instr for kw in ("报告", "请示", "方案", "纪要", "通知")):
            doc_type_map = {"word": "报告", "ppt": "方案",
                            "excel": "报告", "md": "报告"}
            messages.append({"role": "system", "content":
                get_public_doc_system_prompt(
                    a.role, doc_type_map.get(data.doc_type.lower(), "报告"))})
    except Exception as style_err:
        # 风格注入失败不阻塞出稿，但必须出声——否则又会退回"静默失效"
        logger.warning("文档风格注入失败 agent=%s: %s", aid, style_err,
                       exc_info=True)

    messages.append({"role": "user",
                     "content": f"文档标题：{data.title}\n\n要求：{data.instruction}"})

    gw = ModelGateway(db)
    from app.model_gateway import usage_context
    from app.models.agent import AgentModelConfig
    cfg_rows = (await db.execute(select(AgentModelConfig).where(
        AgentModelConfig.agent_id == str(a.id)))).scalars().all()
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    model_ids = [c.model_id for c in sorted(cfg_rows,
                 key=lambda c: order.get(c.role, 3))]
    with usage_context(agent=a, scenario="SKILL", user_id=str(user.id)):
        llm = await gw.chat_with_agent_models(model_ids, messages)
    content = (llm.get("content") or "").strip()

    # 清理可能的 markdown 包裹
    if data.doc_type.lower() != "md" and content.startswith("```"):
        content = re.sub(r"^```\w*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    try:
        result = generate_document(data.doc_type.lower(), data.title,
                                   content, agent_id=str(a.id))
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 登记 Asset（归属该 Agent）
    from app.models.asset import Asset, AssetType
    import mimetypes as _mt
    asset = Asset(
        company_id=a.company_id, name=data.title + spec_ext(data.doc_type),
        type=AssetType.OTHER, path=result["file_path"],
        mime_type=result["mime"], size=result["size"],
        created_by=str(a.id), creator_type="agent", agent_id=str(a.id))
    db.add(asset)
    await _save_msg(db, a, "agent",
                    f"📄 已为你生成{tname}《{data.title}》\n"
                    f"保存位置：我的交付文件夹\n文件：{result['file_name']} ({result['size']//1024}KB)",
                    {"action": "generate_doc", "asset_file": result["file_name"]})
    await db.commit()

    return {"reply": (f"✅ {tname}已生成：《{data.title}》({result['size']//1024}KB)\n"
                      f"文件名：{result['file_name']}\n"
                      f"已存入我的交付文件夹，并登记到成果中心。"),
            "file": result}


def spec_ext(doc_type: str) -> str:
    return {"word": ".docx", "excel": ".xlsx", "ppt": ".pptx", "md": ".md"}.get(doc_type, ".bin")
