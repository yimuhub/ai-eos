"""Agent 工具兼容层：真正的注册表与执行已迁入 app/toolbus/（V6 M1）。

历史说明（为什么这个模块存在）：任务执行 / 私聊 / 群聊过去三处各写一份
`if name == ...`，聊天里能用 web_search，任务链里一个工具都传不下去，
能力包提示词里还写着并不存在的 `browser_fetch`——"会网页浏览"只是提示词
幻觉。V5.4 收敛为单一执行入口 + 统一 schema；V6 M1 把注册、描述、授权
升级为工具总线（app/toolbus/），本模块保留为兼容层：

* `TOOLS` / `TOOL_NAMES` —— 内置 web 工具的 schema（存量调用方不受影响；
  新代码请用 `app.toolbus.resolve_agent_tool_schemas`）；
* `execute_tool` —— 转发到 `toolbus.execute_bus_tool`；
* `parse_text_tool_calls` —— 兼容只会把 tool_call 输出成文本的模型。

安全边界：工具参数来自模型（不可信）。抓取复用 `fetch_page_text`
（协议白名单 + 私网/元数据拒绝 + 逐跳复检），结果截断后才回填上下文。
"""
from __future__ import annotations

import re

from app.toolbus import (MAX_TOOL_CALLS, MAX_TOOL_RESULT_CHARS,
                         MAX_TOOL_ROUNDS, execute_bus_tool, get_tool,
                         tool_schemas)
from app.toolbus.builtin import computer_tools  # noqa: F401 — 注册副作用

# 内置 web 工具的 OpenAI schema（从总线事实源导出，文案与历史版本一致）
TOOLS: list[dict] = tool_schemas(
    [get_tool("web_search"), get_tool("web_fetch")])
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# 兼容别名：聊天链路把电脑工具 schema 拼进自己的 tools 列表用
COMPUTER_TOOL_SCHEMAS = computer_tools.computer_tool_schemas()


async def execute_tool(name: str, args, *, company_id: str | None = None,
                       agent_id: str | None = None,
                       user_id: str | None = None,
                       task_id: str | None = None) -> dict:
    """执行一个工具（转发总线）。错误一律转成结构化结果，不抛给运行时。"""
    return await execute_bus_tool(name, args, company_id=company_id,
                                  agent_id=agent_id, user_id=user_id,
                                  task_id=task_id)


_TEXT_TOOL_RE = re.compile(
    r"<tool_call>\s*<function=([A-Za-z_][A-Za-z0-9_]*)>\s*(\{.*?\})\s*</function>\s*</tool_call>",
    re.S)


def parse_text_tool_calls(text: str) -> list[dict]:
    """解析模型以纯文本输出的 `<tool_call><function=...>` 片段。

    部分模型（如 agnes）不支持结构化 tool_calls，会把调用写成文本。
    不解析就等于工具永远不会被执行，模型只能继续"编"。
    """
    calls: list[dict] = []
    if not text or "<tool_call>" not in text:
        return calls
    for i, m in enumerate(_TEXT_TOOL_RE.finditer(text)):
        calls.append({
            "id": f"text_call_{i}",
            "type": "function",
            "function": {"name": m.group(1), "arguments": m.group(2)},
        })
    return calls


def strip_text_tool_calls(text: str) -> str:
    if not text or "<tool_call>" not in text:
        return text or ""
    return _TEXT_TOOL_RE.sub("", text).strip()


def available_tool_names() -> list[str]:
    return sorted(TOOL_NAMES)


def tool_system_prompt() -> str:
    """给模型的工具使用纪律（与总线 schema 配套）。"""
    return (
        "【工具与取证】你可以调用 web_search（联网搜索）和 web_fetch（抓取网页正文）。"
        "涉及外部事实、市场规模、政策法规、竞品动态、价格与新闻时，"
        "必须先搜索、必要时抓取正文，再基于结果作答；"
        "禁止凭记忆编造数字。引用事实时在正文中标注来源名称，"
        "并在结尾列出「参考来源」中的 URL。"
        "工具返回 error 时应换来源重试或明确说明取数失败，而不是当作成功。"
    )
