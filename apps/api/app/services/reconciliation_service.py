"""对账引擎（V6.1 引擎 4）：规整层双源比对。

典型用法：ERP 里导出的订单（source_a=erp-connector 行 id）对银行/平台
流水（source_b），按 payload.order_no 匹配、比 payload.amount——差异三类：
only_a（有单无款→催收）、only_b（有款无单→查漏）、mismatched（金额不符→
整改）。差异项可驱动催收/整改工作流（行业包模板）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.canonical import CanonicalRecord
from app.models.ops import ReconciliationRun

logger = get_logger(__name__)

_MAX_DIFFS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _load_payload_rows(db: AsyncSession, company_id: str, entity: str,
                             source: str, match_field: str) -> dict[str, dict]:
    """取一个来源的全部行：{匹配键值: 行 payload}。键相同后者覆盖（幂等语义）。

    match_field 是 canonical 路径（如 payload.order_no），落到 payload dict
    内部取值时剥掉根前缀。
    """
    rows = (await db.execute(
        select(CanonicalRecord).where(
            CanonicalRecord.company_id == company_id,
            CanonicalRecord.entity_type == entity,
            CanonicalRecord.source == source))).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        key = _dig_value(r.payload or {}, _relative(match_field))
        if key in (None, ""):
            continue
        out[str(key)] = r.payload or {}
    return out


def _relative(path: str) -> str:
    """payload.xxx → xxx；custom_data.xxx → xxx（对齐 payload dict 内部取值）。"""
    for root in ("payload.", "custom_data."):
        if path.startswith(root):
            return path[len(root):]
    return path


def _dig_value(payload: dict, path: str):
    cur: object = payload
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


async def run_reconciliation(db: AsyncSession, run: ReconciliationRun) -> ReconciliationRun:
    cfg = run.config or {}
    entity = str(cfg.get("entity") or "")
    source_a = str(cfg.get("source_a") or "")
    source_b = str(cfg.get("source_b") or "")
    match_field = str(cfg.get("match_field") or "")
    compare_fields = [str(f) for f in (cfg.get("compare_fields") or [])]
    tolerance = float(cfg.get("amount_tolerance") or 0)
    if not (entity and source_a and source_b and match_field and compare_fields):
        raise ValueError("config 需要 entity/source_a/source_b/"
                         "match_field/compare_fields")

    rows_a = await _load_payload_rows(db, run.company_id, entity, source_a,
                                      match_field)
    rows_b = await _load_payload_rows(db, run.company_id, entity, source_b,
                                      match_field)

    matched, only_a, only_b, mismatched = 0, [], [], []
    for key, pa in rows_a.items():
        pb = rows_b.get(key)
        if pb is None:
            only_a.append({"key": key, "payload": pa})
            continue
        diffs = {}
        for field in compare_fields:
            va, vb = _dig_value(pa, _relative(field)), _dig_value(pb, _relative(field))
            if _num_eq(va, vb, tolerance, field):
                continue
            diffs[field] = {"a": va, "b": vb}
        if diffs:
            mismatched.append({"key": key, "diffs": diffs})
        else:
            matched += 1
    for key, pb in rows_b.items():
        if key not in rows_a:
            only_b.append({"key": key, "payload": pb})

    run.total_a = len(rows_a)
    run.total_b = len(rows_b)
    run.matched = matched
    run.only_a = only_a[:_MAX_DIFFS]
    run.only_b = only_b[:_MAX_DIFFS]
    run.mismatched = mismatched[:_MAX_DIFFS]
    run.status = "success"
    run.finished_at = _now()
    await db.commit()
    logger.info("对账完成 company=%s run=%s matched=%d diffs=%d",
                run.company_id, run.name, matched,
                len(only_a) + len(only_b) + len(mismatched))
    return run


def _num_eq(va, vb, tolerance: float, field: str) -> bool:
    if va == vb:
        return True
    try:
        return abs(float(va) - float(vb)) <= max(tolerance, 1e-9)
    except (TypeError, ValueError):
        return False
