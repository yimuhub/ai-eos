"""内置工具执行器：web_search / web_fetch（从 app/tools.py 迁移，逻辑不变）。

安全边界沿用原实现：工具参数来自模型（不可信），抓取复用 browser_agent 的
SSRF 闸门（协议白名单 + 私网/元数据拒绝 + 逐跳复检）并写审计日志；
技能搜索走 skill_system 的多源降级链。执行器自开短会话，不碰调用方事务。
"""
from __future__ import annotations

import json

from app.core.logging_config import get_logger
from .registry import ToolContext

logger = get_logger(__name__)


async def web_search_executor(args: dict, ctx: ToolContext) -> dict:
    """联网搜索：委托 skill_system 的 WebSearchSkill（多源降级）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "缺少 query"}
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    from app.skill_system import execute_skill
    res = await execute_skill("web_search", {
        "query": query, "max_results": max(1, min(max_results, 10))})
    return res.get("result") or res


async def web_fetch_executor(args: dict, ctx: ToolContext) -> dict:
    """抓取网页正文：独立短会话（fetch_page_text 会写审计并 commit，
    不能把调用方未提交状态一起提交/回滚）。"""
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "缺少 url"}
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "只允许 http/https 地址"}
    try:
        max_chars = int(args.get("max_chars") or 6000)
    except (TypeError, ValueError):
        max_chars = 6000
    from app.db.session import AsyncSessionLocal
    from app.browser_agent import fetch_page_text
    async with AsyncSessionLocal() as tdb:
        page = await fetch_page_text(
            tdb, ctx.company_id or "", ctx.agent_id, url,
            max_chars=max(500, min(max_chars, 12000)))
    if page.get("error"):
        return {"error": page["error"], "url": url}
    if not page.get("allowed", True):
        return {"error": page.get("deny_reason") or "目标地址被拒绝", "url": url}
    return {
        "url": url,
        "title": page.get("title_m") or "",
        "text": page.get("text") or "",
        "rendered": bool(page.get("rendered")),
    }
