"""Agent 工具注册表：任务执行 / 私聊 / 群聊共用同一套定义与执行器。

此前三处各写一份 `if name == ...`：聊天里能用 web_search，任务链里
一个工具都传不下去（`chat_with_agent_models` 根本不带 tools），
能力包提示词里还写着并不存在的 `browser_fetch`。结果就是"会网页浏览"
只是提示词幻觉。本模块收敛为：

* `TOOLS` —— OpenAI function-calling 的 JSON Schema（模型看到的就是它）；
* `execute_tool` —— 唯一执行入口，网络类工具统一走 browser_agent 的
  SSRF 闸门与审计日志；
* `parse_text_tool_calls` —— 兼容只会把 tool_call 输出成文本的模型。

安全边界：工具参数来自模型（不可信）。抓取复用 `fetch_page_text`
（协议白名单 + 私网/元数据拒绝 + 逐跳复检），结果截断后才回填上下文。
"""
from __future__ import annotations

import json
import re

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 单次工具结果回填上限：工具结果会和历史消息一起进下一轮 prompt，
# 不截断会让上下文迅速膨胀（一次抓取 8000 字 × 多轮）。
MAX_TOOL_RESULT_CHARS = 6000
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 12

WEB_SEARCH_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时信息，返回标题、链接、摘要。"
                       "需要外部事实、市场规模、政策、新闻、价格等数据时先搜索再回答。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10,
                                "default": 5},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "抓取指定网页并提取正文纯文本。搜索结果只有摘要，"
                       "写报告前用它对关键来源取正文并核对数字。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 地址"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 12000,
                              "default": 6000},
            },
            "required": ["url"],
        },
    },
}

TOOLS: list[dict] = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def _truncate(value, limit: int = MAX_TOOL_RESULT_CHARS):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


async def execute_tool(name: str, args: dict, *, company_id: str | None = None,
                       agent_id: str | None = None) -> dict:
    """执行一个工具。错误一律转成结构化结果返回给模型，不抛给运行时。

    抛异常会让整个任务失败，而模型完全有能力在"这次抓取失败"后换一个
    来源；把它变成 tool 消息里的 error 字段才是正确的降级方式。
    """
    args = args if isinstance(args, dict) else {}
    try:
        if name == "web_search":
            from app.skill_system import execute_skill
            query = str(args.get("query") or "").strip()
            if not query:
                return {"error": "缺少 query"}
            try:
                max_results = int(args.get("max_results") or 5)
            except (TypeError, ValueError):
                max_results = 5
            res = await execute_skill("web_search", {
                "query": query, "max_results": max(1, min(max_results, 10))})
            return _truncate(res.get("result") or res)

        if name == "web_fetch":
            url = str(args.get("url") or "").strip()
            if not url:
                return {"error": "缺少 url"}
            if not url.lower().startswith(("http://", "https://")):
                return {"error": "只允许 http/https 地址"}
            try:
                max_chars = int(args.get("max_chars") or 6000)
            except (TypeError, ValueError):
                max_chars = 6000
            # 独立短会话：fetch_page_text 会写审计日志并 commit，
            # 不能让它把任务执行中的未提交状态一起提交/回滚。
            from app.db.session import AsyncSessionLocal
            from app.browser_agent import fetch_page_text
            async with AsyncSessionLocal() as tdb:
                page = await fetch_page_text(
                    tdb, company_id or "", agent_id, url,
                    max_chars=max(500, min(max_chars, 12000)))
            if page.get("error"):
                return {"error": page["error"], "url": url}
            if not page.get("allowed", True):
                return {"error": page.get("deny_reason") or "目标地址被拒绝",
                        "url": url}
            return _truncate({
                "url": url,
                "title": page.get("title_m") or "",
                "text": page.get("text") or "",
                "rendered": bool(page.get("rendered")),
            })

        return {"error": f"未知工具: {name}"}
    except Exception as exc:  # 工具失败不拖垮任务
        logger.warning("工具执行失败 name=%s: %s", name, exc, exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


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
    """给模型的工具使用纪律（与 TOOLS 的 schema 配套）。"""
    return (
        "【工具与取证】你可以调用 web_search（联网搜索）和 web_fetch（抓取网页正文）。"
        "涉及外部事实、市场规模、政策法规、竞品动态、价格与新闻时，"
        "必须先搜索、必要时抓取正文，再基于结果作答；"
        "禁止凭记忆编造数字。引用事实时在正文中标注来源名称，"
        "并在结尾列出「参考来源」中的 URL。"
        "工具返回 error 时应换来源重试或明确说明取数失败，而不是当作成功。"
    )
