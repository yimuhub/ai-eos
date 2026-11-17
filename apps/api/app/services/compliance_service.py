"""合规台账服务（V6.1 引擎 1）：状态刷新 + 行业包播种 + 到期预警数据源。

到期预警的数据流：automation 定期调 `refresh_company_status` → 状态
落库（expiring/expired）→ briefing/看板消费；责任人派单由 owner_agent_id
指向的数字员工承接。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.ops import ComplianceLedger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def derive_status(expires_at: datetime | None,
                  days_before: int) -> str:
    """有效/临期/过期/手工登记（无有效期）。"""
    if expires_at is None:
        return "manual"
    now = _now()
    if expires_at < now:
        return "expired"
    if expires_at <= now + timedelta(days=days_before):
        return "expiring"
    return "valid"


async def refresh_company_status(db: AsyncSession, company_id: str) -> dict:
    """刷新一家公司全部台账的派生状态；返回各级计数。"""
    rows = (await db.execute(
        select(ComplianceLedger).where(
            ComplianceLedger.company_id == company_id))).scalars().all()
    counts = {"valid": 0, "expiring": 0, "expired": 0, "manual": 0}
    for row in rows:
        new_status = derive_status(row.expires_at, row.renewal_days_before)
        row.status = new_status
        counts[new_status] += 1
        row.last_checked_at = _now()
    await db.commit()
    logger.info("合规台账状态刷新 company=%s %s", company_id, counts)
    return counts


async def seed_from_pack(db: AsyncSession, company_id: str, pack_id: str,
                         items: list[dict], user_id: str | None) -> int:
    """行业包安装时按 compliance_items 播种台账（幂等：按 company+key）。"""
    from .industry_pack_service import validate_content  # noqa: F401 引用校验语义

    created = 0
    for item in items:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        exists = (await db.execute(
            select(ComplianceLedger.id).where(
                ComplianceLedger.company_id == company_id,
                ComplianceLedger.key == key))).first()
        if exists:
            continue
        db.add(ComplianceLedger(
            id=_uuid(), company_id=company_id, key=key,
            name=str(item.get("name") or key)[:200],
            authority=item.get("authority"),
            status="manual",
            renewal_rule=item.get("renewal_rule"),
            renewal_days_before=max(1, int(item.get("renewal_days_before") or 30)),
            penalty=item.get("penalty"),
            evidence_note=item.get("evidence_note"),
            pack_id=pack_id, created_by=user_id))
        created += 1
    await db.commit()
    return created


def ledger_out(row: ComplianceLedger) -> dict:
    return {"id": row.id, "key": row.key, "name": row.name,
            "authority": row.authority, "status": row.status,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "renewal_rule": row.renewal_rule,
            "renewal_days_before": row.renewal_days_before,
            "penalty": row.penalty, "evidence_note": row.evidence_note,
            "owner_agent_id": row.owner_agent_id, "pack_id": row.pack_id,
            "last_checked_at": (row.last_checked_at.isoformat()
                                if row.last_checked_at else None)}


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())
