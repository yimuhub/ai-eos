"""Webhook 连接器：外部系统主动推数（V6 M3）。

适用：企微/钉钉/飞书的回调、自建系统的变更推送。收到的行直接走
map_row 映射（复用同一套 mapping/id_field 配置），幂等 upsert。

v1 边界（如实）：入站鉴权走 AI-EOS 会话或签名头二选一——真正的
外部回调应配置签名密钥（config.webhook_secret_name → 保险库），
本版本先支持会话鉴权 + HMAC 校验骨架，免签会话模式面向内部系统。
"""
from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.toolbus.dispatch import _resolve_bearer
from .base import PullRecord, PullResult, map_row

logger = get_logger(__name__)


class WebhookConnector:
    kind = "webhook"

    async def ingest(self, db: AsyncSession, row, body: bytes,
                     signature: str | None = None) -> list[PullRecord]:
        """把推送体转成 PullRecord 列表（数组或单对象均可）。

        签名模式：config.webhook_secret_name 指向保险库密钥，按
        HMAC-SHA256(body) 十六进制比对（x-signature 头传值）。
        """
        cfg = dict(row.config or {})
        secret_name = cfg.get("webhook_secret_name")
        if secret_name:
            expected_key = await _resolve_bearer(str(row.company_id),
                                                 secret_name)
            if not expected_key or not signature:
                raise PermissionError("缺少签名或签名密钥未配置")
            expected = hmac.new(expected_key.encode(), body,
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, (signature or "").lower()):
                raise PermissionError("签名校验失败")

        data = json.loads(body.decode("utf-8"))
        items = data if isinstance(data, list) else [data]
        records: list[PullRecord] = []
        for raw in items:
            if isinstance(raw, dict):
                rec = map_row(raw, cfg)
                if rec is not None:
                    records.append(rec)
        return records

    async def pull(self, db: AsyncSession, row, watermark, limit=0) -> PullResult:
        # push 型连接器没有 pull：返回空由 run_sync 记录"无拉取"语义
        return PullResult()
