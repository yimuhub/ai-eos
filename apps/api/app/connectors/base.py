"""连接器 SDK（V6 M3）：外部管理软件 → 规整层的统一入口。

接口只有一个动作：`pull`（增量拉取），返回一批规范记录 + 新水位。
配置全部挂在 tool_registry 行的 config JSON 里（kind / entity_type /
mapping / 分页 / 水位字段），连接器实现不持有状态。

上游安全约定：
* REST/MCP 出站统一走 http_client + 公网闸门（与 web_fetch/工具总线
  同一威胁模型）；
* 凭据经 secret_vault（auth_secret_ref），明文不落 config。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PullRecord:
    """一条待规整的外部记录。"""
    external_id: str
    payload: dict = field(default_factory=dict)   # canonical 字段
    raw: dict | None = None                        # 原始行（排障）
    source_updated_at: datetime | None = None


@dataclass
class PullResult:
    records: list[PullRecord] = field(default_factory=list)
    watermark: str | None = None
    has_more: bool = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_dt(value: Any) -> datetime | None:
    """尽力解析源系统时间（ISO/时间戳秒），失败返回 None 不炸同步。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 4].replace("Z", ""),
                                     fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def map_row(row: dict, cfg: dict) -> PullRecord | None:
    """按配置映射一行原始数据 → PullRecord。

    cfg: {id_field, updated_field?, mapping: {canonical_key: raw_key}}
    映射缺失的键在 payload 里显式置 None（宁可有 None 也不静默丢字段）。
    """
    id_field = str(cfg.get("id_field") or "id")
    external_id = row.get(id_field)
    if external_id is None or str(external_id) == "":
        return None
    payload: dict = {}
    for canonical_key, raw_key in (cfg.get("mapping") or {}).items():
        payload[canonical_key] = row.get(str(raw_key))
    updated = parse_dt(row.get(cfg.get("updated_field"))) \
        if cfg.get("updated_field") else None
    return PullRecord(external_id=str(external_id)[:200], payload=payload,
                      raw=row, source_updated_at=updated)


async def upsert_records(db: AsyncSession, *, company_id: str, source: str,
                         entity_type: str, records: list[PullRecord]) -> int:
    """幂等落规整层：按 (company, entity, source, external_id) upsert。

    返回 upsert 的行数。每次同步更新 payload/raw/seen_at；source_updated_at
    只在新值非空时覆盖（老系统偶尔回传空时间，别把已有水位抹了）。
    """
    from sqlalchemy import select
    from app.models.canonical import CanonicalRecord

    now = utcnow()
    seen_ids = [r.external_id for r in records]
    existing = {}
    if seen_ids:
        rows = (await db.execute(
            select(CanonicalRecord).where(
                CanonicalRecord.company_id == company_id,
                CanonicalRecord.entity_type == entity_type,
                CanonicalRecord.source == source,
                CanonicalRecord.external_id.in_(seen_ids[:500])))).scalars().all()
        existing = {r.external_id: r for r in rows}

    count = 0
    inserted: dict[str, Any] = {}  # 本批内已新建的行（同一 id 可能在一批里出现多次）
    for rec in records:
        row = existing.get(rec.external_id) or inserted.get(rec.external_id)
        if row is None:
            row = CanonicalRecord(
                company_id=company_id, entity_type=entity_type,
                source=source, external_id=rec.external_id,
                payload=rec.payload, raw=rec.raw, seen_at=now,
                source_updated_at=rec.source_updated_at)
            db.add(row)
            inserted[rec.external_id] = row
        else:
            row.payload = rec.payload
            row.raw = rec.raw
            row.seen_at = now
            if rec.source_updated_at is not None:
                row.source_updated_at = rec.source_updated_at
        count += 1
    await db.flush()
    return count


def get_connector(kind: str):
    """按 kind 取连接器实现（懒加载避免环）。"""
    if kind == "rest":
        from .rest_api import RestApiConnector
        return RestApiConnector()
    if kind == "webhook":
        from .webhook import WebhookConnector
        return WebhookConnector()
    if kind == "mcp_bridge":
        from .mcp_bridge import McpBridgeConnector
        return McpBridgeConnector()
    raise ValueError(f"未知连接器类型 {kind}（支持 rest/webhook/mcp_bridge）")
