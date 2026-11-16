"""工具总线（V6 M1）：AI-EOS 一切工具的统一注册、描述、授权与执行。

对外主入口：
* `execute_bus_tool` —— 唯一执行入口（内置/电脑/DB 动态工具统一路由）；
* `resolve_agent_tool_schemas` —— 按 Agent 解析可下发 schema（模型看到的
  = 真的能执行的）；
* `get_tool` / `all_incode_tools` —— 进程内注册表查询；
* `spec_from_row` —— tool_registry 行 → 运行时 spec。

`builtin` 在包导入时即注册 web + computer 工具（import 副作用，与
skill_system 的装饰器注册同一风格）。
"""
from .registry import (MAX_TOOL_CALLS, MAX_TOOL_RESULT_CHARS, MAX_TOOL_ROUNDS,
                       ToolContext, ToolSpec, all_incode_tools, get_tool,
                       has_db_tool, mcp_descriptors, mcp_descriptor,
                       openai_schema, register_tool, replace_tool,
                       resolve_agent_tools, resolve_agent_tool_schemas,
                       spec_from_row, tool_schemas, tool_prompt_section)
from .dispatch import execute_bus_tool, execute_db_tool
from . import builtin  # noqa: F401  — import 即注册内置工具

__all__ = [
    "MAX_TOOL_CALLS", "MAX_TOOL_RESULT_CHARS", "MAX_TOOL_ROUNDS",
    "ToolContext", "ToolSpec", "all_incode_tools", "get_tool", "has_db_tool",
    "mcp_descriptors", "mcp_descriptor", "openai_schema", "register_tool",
    "replace_tool", "resolve_agent_tools", "resolve_agent_tool_schemas",
    "spec_from_row", "tool_schemas", "tool_prompt_section",
    "execute_bus_tool", "execute_db_tool",
]
