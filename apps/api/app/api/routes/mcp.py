"""MCP 双向集成（V5.5 B2）。

出站（暴露）：把 AI-EOS 引擎作为 MCP Server 暴露——外部 Agent
（Claude Code / Codex / 扣子等）通过 JSON-RPC 2.0 调用 tools：
  - aieos.create_task    在你的 AI 公司里建任务并指派给某员工
  - aieos.list_tasks     查任务状态
  - aieos.task_trace     看某任务的节点级执行轨迹
  - aieos.run_workflow   触发一个工作流
  - aieos.search_kb      检索公司知识库
  - aieos.get_briefing   拉 CEO 晨间简报
  - aieos.list_agents    列出数字员工

入站（接入）：ExternalAgentConnection 支持 MCP_HTTP provider——
把外部 MCP Server 的 tools/call 直通给该连接（execute 动作的
task_input = JSON {"tool": "...", "arguments": {...}}）。
"""
import json
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
from app.models.user import User
from app.version import __version__

router = APIRouter(prefix="/mcp", tags=["mcp"])

MCP_PROTOCOL_VERSION = "2025-06-18"

# ---------------- tools 定义（出站） ----------------
TOOLS = [
    {"name": "aieos.create_task",
     "description": "在 AI-EOS 数字公司里创建任务并可指派给某位数字员工（按名字）。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "title": {"type": "string"},
                         "description": {"type": "string"},
                         "agent_name": {"type": "string",
                                         "description": "可选：指派给哪位员工（按名字匹配）"}},
                     "required": ["title"]}},
    {"name": "aieos.list_tasks",
     "description": "列出公司任务（可按状态过滤：TODO/RUNNING/PAUSED/COMPLETED/FAILED/CANCELLED）。",
     "inputSchema": {"type": "object",
                     "properties": {"status": {"type": "string"},
                                     "limit": {"type": "integer"}}}},
    {"name": "aieos.task_trace",
     "description": "查看某任务的节点级执行轨迹（每 Agent 每步 + token 成本 + 检查点）。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"]}},
    {"name": "aieos.run_workflow",
     "description": "触发公司里的一条工作流（按 id 或名字），后台执行。",
     "inputSchema": {"type": "object",
                      "properties": {"workflow": {"type": "string",
                                                  "description": "工作流 id 或名字"}},
                      "required": ["workflow"]}},
    {"name": "aieos.search_kb",
     "description": "检索公司知识库（两级 RAG）。",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "aieos.get_briefing",
     "description": "拉取 CEO 晨间简报（昨日完成/今日计划/需要你决策/今日成本）。",
     "inputSchema": {"type": "object"}},
    {"name": "aieos.list_agents",
     "description": "列出公司数字员工（名字/角色/状态）。",
     "inputSchema": {"type": "object"}},
]


class McpCallIn(BaseModel):
    jsonrpc: str = "2.0"
    id: object = None
    method: str
    params: dict | None = None


def _text_result(rpc_id, text: str, err: str | None = None):
    """MCP tools/call 结果包装（content[0].text）。"""
    payload = {"content": [{"type": "text", "text": text}],
               "isError": bool(err)}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


async def _user_company(db: AsyncSession, user: User, company_id: str | None):
    """MCP 请求默认用用户首家公司；也可通过 X-AI-EOS-Company 头指定。"""
    from app.models.company import Company
    if company_id:
        await assert_company_owned(db, user.id, company_id)
        return company_id
    rows = (await db.execute(
        select(Company).where(Company.owner_id == user.id).limit(1)
    )).scalars().all()
    return rows[0].id if rows else None


async def _execute_tool(db: AsyncSession, user: User, company_id: str,
                       name: str, args: dict) -> tuple[str, str | None]:
    """执行 MCP tool。返回 (text, error)。"""
    try:
        if name == "aieos.create_task":
            from app.models.task import Task
            from app.models.agent import Agent
            data = {"title": args["title"], "company_id": company_id}
            if args.get("description"):
                data["description"] = args["description"]
            agent = None
            if args.get("agent_name"):
                ar = (await db.execute(select(Agent).where(
                    Agent.company_id == company_id,
                    Agent.name == args["agent_name"]))).scalars().first()
                agent = ar
            t = Task(**{k: v for k, v in data.items() if v is not None},
                     assigned_agent_id=agent.id if agent else None,
                     created_by=user.id)
            db.add(t)
            await db.commit()
            await db.refresh(t)
            return (f"任务已创建：id={t.id}「{t.title}」"
                    f"{'（指派给 ' + agent.name + '）' if agent else ''}，"
                    f"可到 AI-EOS 指挥中心跟踪。"), None

        if name == "aieos.list_tasks":
            from app.models.task import Task, TaskStatus
            q = select(Task).where(Task.company_id == company_id)
            if args.get("status"):
                q = q.where(Task.status == TaskStatus(args["status"]))
            rows = (await db.execute(
                q.order_by(Task.created_at.desc())
                .limit(int(args.get("limit") or 20)))).scalars().all()
            lines = [f"- [{t.status.value}] {t.title}（id={t.id}）" for t in rows]
            return "\n".join(lines) or "（没有匹配的任务）", None

        if name == "aieos.task_trace":
            from app.api.routes.tasks import task_trace
            trace = await task_trace(args["task_id"], db=db, user=user)
            return json.dumps(trace, ensure_ascii=False, default=str), None

        if name == "aieos.run_workflow":
            from app.models.workflow import (WorkflowDefinition,
                                              WorkflowStatus)
            key = args["workflow"]
            wf = None
            wr = (await db.execute(select(WorkflowDefinition).where(
                WorkflowDefinition.company_id == company_id,
                WorkflowDefinition.id == key))).scalars().first()
            if not wr:
                wr = (await db.execute(select(WorkflowDefinition).where(
                    WorkflowDefinition.company_id == company_id,
                    WorkflowDefinition.name == key))).scalars().first()
            wf = wr
            if not wf or wf.status == WorkflowStatus.ARCHIVED:
                return "", f"找不到可执行的工作流「{key}」（或已归档）"
            from app.api.routes.workflows import (start_execution,
                                                  _run_in_background)
            ex = await start_execution(db, wf.id, None, user.id)
            _run_in_background(ex.id)
            return (f"工作流「{wf.name}」已触发（后台执行）\n"
                    f"execution_id={ex.id}"), None

        if name == "aieos.search_kb":
            from app.kb_engine import cost_optimized_rag
            rag = await cost_optimized_rag(db, company_id, None,
                                            args["query"], agent=None)
            txt = rag.get("context_text") or "（知识库没有相关内容）"
            return txt[:4000], None

        if name == "aieos.get_briefing":
            from app.models.company import Company
            from app.api.routes.briefing import build_briefing
            company = await db.get(Company, company_id)
            brief = await build_briefing(db, company)
            return json.dumps(brief, ensure_ascii=False, default=str), None

        if name == "aieos.list_agents":
            from app.models.agent import Agent
            rows = (await db.execute(
                select(Agent).where(Agent.company_id == company_id)
                .order_by(Agent.created_at.asc()))).scalars().all()
            lines = [f"- {a.name}（{a.role or '员工'}，{a.status.value}）"
                     for a in rows]
            return "\n".join(lines) or "（还没有数字员工）", None

        return "", f"未知工具 {name}"
    except Exception as exc:
        return "", f"{type(exc).__name__}: {str(exc)[:300]}"


@router.get("")
async def mcp_capabilities(request: Request,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """能力发现页（给外部 Agent 看：有哪些 tools 可调用）。"""
    return {"protocol": "MCP", "version": MCP_PROTOCOL_VERSION,
            "endpoint": "POST /mcp（JSON-RPC 2.0）",
            "auth": "Authorization: Bearer <AI-EOS access token>",
            "optional_header": "X-AI-EOS-Company: <company_id>",
            "tools": TOOLS}


@router.post("")
async def mcp_rpc(data: McpCallIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """MCP JSON-RPC 2.0 入口（streamable-http 单请求模式）。"""
    method = data.method
    rpc_id = data.id
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ai-eos", "version": __version__}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id,
                "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = data.params or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        cid = request.headers.get("X-AI-EOS-Company")
        cid = await _user_company(db, user, cid)
        if not cid:
            return _text_result(rpc_id, "还没有公司，无法执行",
                                "no company")
        text, err = await _execute_tool(db, user, cid, name, args)
        return _text_result(rpc_id, text, err)
    if method in ("ping",):
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


# ---------------- 入站：外部 MCP Server 直通（execute 通道） ----------------
async def mcp_http_execute(endpoint: str, task_input: str,
                           config: dict | None, bearer: str | None = None,
                           timeout: float = 30.0) -> dict:
    """调外部 MCP Server 的 tools/call。

    task_input 支持两种形式：
      1. JSON：{"tool": "x", "arguments": {...}}（显式）
      2. 自然语言：先 tools/list 找工具，取第一个能匹配的（尽力而为）
    """
    import httpx

    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    def _rpc(method: str, params: dict | None = None, rid=1):
        return {"jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {}}

    async with httpx.AsyncClient(timeout=timeout) as client:
        body = task_input.strip()
        explicit = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "tool" in parsed:
                explicit = parsed
        except (json.JSONDecodeError, TypeError):
            explicit = None

        if explicit:
            rpc = _rpc("tools/call",
                       {"name": explicit["tool"],
                        "arguments": explicit.get("arguments") or {}})
            r = await client.post(endpoint, json=rpc, headers=headers)
            r.raise_for_status()
            # 兼容 SSE 帧（text/event-stream）
            ct = r.headers.get("content-type", "")
            if "text/event-stream" in ct:
                line = next((ln for ln in r.text.splitlines()
                              if ln.startswith("data:")), r.text)
                data = json.loads(line[len("data:"):].strip()
                                  or "{}")
            else:
                data = r.json()
            return {"result": data.get("result", data),
                    "tool": explicit["tool"]}

        # 自然语言：tools/list → 挑第一个工具调（外部 MCP 一般工具少，够用）
        r = await client.post(endpoint, json=_rpc("tools/list"),
                              headers=headers)
        r.raise_for_status()
        data = r.json()
        tools = ((data.get("result") or {}).get("tools") or [])
        if not tools:
            return {"result": "外部 MCP Server 没有可用工具", "tool": None}
        tool = tools[0]
        args = {"query": body}
        r = await client.post(endpoint, json=_rpc("tools/call",
            {"name": tool["name"], "arguments": args}), headers=headers)
        r.raise_for_status()
        data2 = r.json()
        return {"result": data2.get("result", data2),
                "tool": tool.get("name"),
                "note": "自然语言模式：已用该工具第一个匹配项执行"}
