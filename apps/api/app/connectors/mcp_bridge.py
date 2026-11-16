"""MCP 桥连接器：把任何 MCP Server 当数据源（V6 M3）。

Odoo 20 / ERPNext / Composio 等生态已经 MCP 化——对接成本≈一次配置。
实现：tools/call 远端工具，行数组走 mapping（与 REST 同一套映射配置）。

config 增量字段：
{
  "kind": "mcp_bridge",
  "tool": "odoo_search_records",     // 远端工具名（默认 = source_ref/name）
  "query_arguments": {"model": "sale.order"},  // 固定参数（与页参数合并）
  "data_path": "content.0.text"      // 结果里 JSON 行数组所在路径
}
"""
from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.toolbus.dispatch import _assert_public_http_url, _resolve_bearer
from .base import PullRecord, PullResult, map_row
from .rest_api import _dig

logger = get_logger(__name__)


class McpBridgeConnector:
    kind = "mcp_bridge"

    async def pull(self, db: AsyncSession, row, watermark, limit=500) -> PullResult:
        cfg = dict(row.config or {})
        endpoint = str(row.endpoint or "").strip()
        _assert_public_http_url(endpoint)
        tool = str(cfg.get("tool") or row.source_ref or row.name)
        arguments = dict(cfg.get("query_arguments") or {})
        if watermark:
            arguments.setdefault("modified_since", watermark)
        arguments.setdefault("limit", min(int(cfg.get("page_size") or 200),
                                          limit))

        from app.core.http_client import http_client
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        bearer = await _resolve_bearer(str(row.company_id),
                                       cfg.get("auth_secret_ref")
                                       or row.auth_secret_ref)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        def _rpc(method, params=None, rid=1):
            return {"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}}

        records: list[PullRecord] = []
        async with http_client(timeout=float(row.timeout_seconds or 60)) as client:
            resp = await client.post(endpoint, headers=headers, json=_rpc(
                "tools/call", {"name": tool, "arguments": arguments}))
            resp.raise_for_status()
            if "text/event-stream" in resp.headers.get("content-type", ""):
                line = next((ln for ln in resp.text.splitlines()
                             if ln.startswith("data:")), "{}")
                data = json.loads(line[len("data:"):].strip() or "{}")
            else:
                data = resp.json()
        result = data.get("result") or data
        # MCP 文本结果里可能内嵌 JSON 行数组：先试 data_path 挖，再试整体解析
        rows = _dig(result, cfg.get("data_path") or "")
        if rows is None:
            for item in (result.get("content") or []):
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        parsed = json.loads(item.get("text") or "")
                        rows = parsed if isinstance(parsed, list) else None
                    except (json.JSONDecodeError, ValueError):
                        rows = None
                    if rows:
                        break
        if isinstance(rows, list):
            for raw in rows:
                if isinstance(raw, dict):
                    rec = map_row(raw, cfg)
                    if rec is not None:
                        records.append(rec)
        return PullResult(records=records, watermark=watermark,
                          has_more=bool(records) and len(records) >= limit)
