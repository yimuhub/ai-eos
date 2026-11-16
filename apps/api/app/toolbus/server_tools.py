"""AI-EOS 对外 MCP Server 的工具集（aieos.*）。

这些工具是「基座本身」暴露给外部 Agent（Claude Code / Codex / 扣子等）
的能力：create_task / list_tasks / task_trace / run_workflow / search_kb /
get_briefing / list_agents。schema 从 mcp.py 迁来逐字一致。

执行不在这里：`mcp.py` 的 `_execute_tool` 持有用户上下文（build_ctx 行级
范围），这些 spec 只负责描述。新基座工具（如 M3 的 metrics.query）要在
对外的 tools/list 里出现时，加一个 expose_mcp=True 的 spec 即可。

注意边界：这些工具**不会**自动下发给自家员工（resolve_agent_tools 只认
builtin/computer/db 三类来源）——内外两面分开是刻意的。
"""
from __future__ import annotations

from .registry import ToolSpec, mcp_descriptors

SERVER_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="aieos.create_task",
        description="在 AI-EOS 数字公司里创建任务并可指派给某位数字员工（按名字）。",
        parameters={"type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "agent_name": {"type": "string",
                                       "description": "可选：指派给哪位员工（按名字匹配）"}},
                    "required": ["title"]},
        source="mcp_server", risk="medium", required_capability="operate",
        expose_mcp=True),
    ToolSpec(
        name="aieos.list_tasks",
        description="列出公司任务（可按状态过滤：TODO/RUNNING/PAUSED/COMPLETED/FAILED/CANCELLED）。",
        parameters={"type": "object",
                    "properties": {"status": {"type": "string"},
                                   "limit": {"type": "integer"}}},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
    ToolSpec(
        name="aieos.task_trace",
        description="查看某任务的节点级执行轨迹（每 Agent 每步 + token 成本 + 检查点）。",
        parameters={"type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"]},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
    ToolSpec(
        name="aieos.run_workflow",
        description="触发公司里的一条工作流（按 id 或名字），后台执行。",
        parameters={"type": "object",
                    "properties": {"workflow": {"type": "string",
                                                "description": "工作流 id 或名字"}},
                    "required": ["workflow"]},
        source="mcp_server", risk="medium", required_capability="operate",
        expose_mcp=True),
    ToolSpec(
        name="aieos.search_kb",
        description="检索公司知识库（两级 RAG）。",
        parameters={"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
    ToolSpec(
        name="aieos.query_metric",
        description="查询公司指标（销售额/应收账龄/良品率等已定义指标）。"
                    "问业务数字时用这个，不要估算。",
        parameters={"type": "object",
                    "properties": {"key": {"type": "string",
                                           "description": "指标 key"}},
                    "required": ["key"]},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
    ToolSpec(
        name="aieos.get_briefing",
        description="拉取 CEO 晨间简报（昨日完成/今日计划/需要你决策/今日成本）。",
        parameters={"type": "object", "properties": {}},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
    ToolSpec(
        name="aieos.list_agents",
        description="列出公司数字员工（名字/角色/状态）。",
        parameters={"type": "object", "properties": {}},
        source="mcp_server", risk="low", required_capability="view",
        expose_mcp=True),
]


def server_mcp_descriptors() -> list[dict]:
    return mcp_descriptors([s for s in SERVER_TOOL_SPECS if s.expose_mcp])
