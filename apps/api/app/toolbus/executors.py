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


async def metrics_query_executor(args: dict, ctx: ToolContext) -> dict:
    """指标查询：规整层 + 指标注册表，AI 取数的唯一正门。"""
    key = str(args.get("key") or "").strip()
    if not key:
        return {"error": "缺少 key"}
    if not ctx.company_id:
        return {"error": "指标查询需要公司上下文"}
    from app.db.session import AsyncSessionLocal
    from app.services.metrics_service import evaluate_metric
    async with AsyncSessionLocal() as mdb:
        try:
            return await evaluate_metric(mdb, ctx.company_id, key)
        except LookupError as exc:
            return {"error": str(exc),
                    "hint": "请让老板先在指标中心定义该指标（/metrics）"}


async def web_search_executor(args: dict, ctx: ToolContext) -> dict:
    """联网搜索：委托 skill_system 的 WebSearchSkill（多源降级）。
    结果是不可信外部内容：逐条过注入防护并附防御指令（V6.2 安全债）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "缺少 query"}
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    from app.skill_system import execute_skill
    from app.toolbus.security import (sanitize_untrusted_text,
                                      web_instruction, wrap_untrusted)
    res = await execute_skill("web_search", {
        "query": query, "max_results": max(1, min(max_results, 10))})
    result = res.get("result") or res
    # 结果形状兜底：list 或 {results: list} 都支持
    items = result if isinstance(result, list) else (result or {}).get("results") or []
    filtered = 0
    cleaned_items: list = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            it = dict(it)
            for field in ("content", "snippet", "title", "text", "summary"):
                if isinstance(it.get(field), str):
                    it[field], n = sanitize_untrusted_text(it[field], 3000)
                    filtered += n
        elif isinstance(it, str):
            it, n = sanitize_untrusted_text(it, 3000)
            filtered += n
        cleaned_items.append(it)
    if isinstance(result, list):
        out: dict = {"results": cleaned_items}
    else:
        out = dict(result or {})
        out["results"] = cleaned_items
    out["external_content"] = wrap_untrusted(f"web_search:{query}", {
        "instruction": web_instruction(), "filtered": filtered})
    return out


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
    from app.toolbus.security import (sanitize_untrusted_text, web_instruction,
                                      wrap_untrusted)
    async with AsyncSessionLocal() as tdb:
        page = await fetch_page_text(
            tdb, ctx.company_id or "", ctx.agent_id, url,
            max_chars=max(500, min(max_chars, 12000)))
    if page.get("error"):
        return {"error": page["error"], "url": url}
    if not page.get("allowed", True):
        return {"error": page.get("deny_reason") or "目标地址被拒绝", "url": url}
    text, filtered = sanitize_untrusted_text(page.get("text") or "",
                                             max(500, min(max_chars, 12000)))
    return {
        "url": url,
        "title": page.get("title_m") or "",
        "text": text,
        "rendered": bool(page.get("rendered")),
        "external_content": wrap_untrusted(url, {
            "instruction": web_instruction(), "filtered": filtered}),
    }
