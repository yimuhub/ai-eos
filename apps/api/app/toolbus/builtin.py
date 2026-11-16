"""内置工具注册：import 即生效（web 两件 + 电脑 15 件）。

web 工具的 description/parameters 与 app/tools.py 历史版本逐字一致——
存量测试与模型行为都不应有感知。电脑工具见 computer_tools.py。
"""
from __future__ import annotations

from . import computer_tools
from .executors import web_fetch_executor, web_search_executor
from .registry import ToolSpec, register_tool


def _register_builtin() -> None:
    register_tool(ToolSpec(
        name="web_search",
        description="联网搜索实时信息，返回标题、链接、摘要。"
                    "需要外部事实、市场规模、政策、新闻、价格等数据时先搜索再回答。",
        parameters={"type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "max_results": {"type": "integer", "minimum": 1,
                                        "maximum": 10, "default": 5},
                    },
                    "required": ["query"]},
        source="builtin", risk="low", required_capability="view",
        category="web", display_name="联网搜索",
        prompt_hint="涉及外部事实、价格、政策时先搜索，不要凭记忆编数字。",
        executor=web_search_executor))
    register_tool(ToolSpec(
        name="web_fetch",
        description="抓取指定网页并提取正文纯文本。搜索结果只有摘要，"
                    "写报告前用它对关键来源取正文并核对数字。",
        parameters={"type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http/https 地址"},
                        "max_chars": {"type": "integer", "minimum": 500,
                                      "maximum": 12000, "default": 6000},
                    },
                    "required": ["url"]},
        source="builtin", risk="low", required_capability="view",
        category="web", display_name="网页正文抓取",
        prompt_hint="写报告前对关键来源抓正文核对数字；引用标注来源 URL。",
        executor=web_fetch_executor))

    computer_tools.register_computer_tools()


_register_builtin()
