"""通用 REST 连接器：配置驱动的外部 API 拉取（V6 M3）。

config 形态（挂在 tool_registry.config）：
{
  "kind": "rest", "entity_type": "order",
  "id_field": "bill_no", "updated_field": "modified_time",
  "mapping": {"order_no": "bill_no", "customer": "cust_name"},
  "endpoint": "https://api.example.com",
  "list_path": "/api/orders", "method": "GET",
  "auth_secret_ref": "kingdee-token",       // 可选，Bearer
  "headers": {"X-App": "aieos"},
  "data_path": "data.list",                  // 响应里数组所在路径（点号）
  "page_param": "page", "size_param": "size", "page_size": 100,
  "max_pages": 10, "watermark_param": "modified_since"
}

v1 认证样式：Bearer（保险库）；金蝶/用友的 app 签名认证后续做成专用
子类（ signing 参数各有差异，不值得塞进通用配置）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.toolbus.dispatch import _assert_public_http_url, _resolve_bearer
from .base import PullRecord, PullResult, map_row

logger = get_logger(__name__)


def _dig(data: Any, path: str) -> Any:
    cur = data
    for seg in [s for s in str(path or "").split(".") if s]:
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.lstrip("-").isdigit():
            try:
                cur = cur[int(seg)]
            except IndexError:
                return None
        else:
            return None
    return cur


class RestApiConnector:
    kind = "rest"

    async def pull(self, db: AsyncSession, row, watermark: str | None,
                   limit: int = 500) -> PullResult:
        cfg = dict(row.config or {})
        endpoint = str(cfg.get("endpoint") or "").strip()
        list_path = str(cfg.get("list_path") or "").strip()
        _assert_public_http_url(endpoint + list_path)  # 执行档：真解析
        method = str(cfg.get("method") or "GET").upper()
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        bearer = await _resolve_bearer(str(row.company_id),
                                       cfg.get("auth_secret_ref"))
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        page_param = cfg.get("page_param") or "page"
        size_param = cfg.get("size_param") or "size"
        page_size = min(max(1, int(cfg.get("page_size") or 100)), 500)
        max_pages = min(max(1, int(cfg.get("max_pages") or 5)), 50)
        watermark_param = cfg.get("watermark_param")

        from app.core.http_client import http_client
        records: list[PullRecord] = []
        new_watermark = watermark
        has_more = False
        total_budget = limit
        async with http_client(timeout=30) as client:
            for page in range(1, max_pages + 1):
                params = {page_param: page, size_param: page_size}
                if watermark_param and watermark:
                    params[watermark_param] = watermark
                resp = await client.request(
                    method, endpoint + list_path, headers=headers,
                    params=params,
                    json=None if method == "GET" else cfg.get("body"))
                resp.raise_for_status()
                data = resp.json()
                rows = _dig(data, cfg.get("data_path") or "")
                if rows is None:
                    rows = data if isinstance(data, list) else []
                if not isinstance(rows, list):
                    break
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    rec = map_row(raw, cfg)
                    if rec is not None:
                        records.append(rec)
                        if cfg.get("updated_field"):
                            ts = raw.get(cfg["updated_field"])
                            if ts is not None and str(ts) >= str(
                                    new_watermark or ""):
                                new_watermark = str(ts)
                total_budget -= len(rows)
                if len(rows) < page_size or total_budget <= 0:
                    has_more = bool(rows and len(rows) >= page_size
                                    and total_budget <= 0 and page < max_pages)
                    break
                has_more = True
        return PullResult(records=records, watermark=new_watermark,
                          has_more=has_more)
