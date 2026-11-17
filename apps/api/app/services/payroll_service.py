"""工资/分佣引擎（V6.1 引擎 5）：四种模式的月度核算。

口径来源是规整层（canonical_records）——计件看 workorder 的 qty、提成看
order 的 amount，聚合字段路径复用指标层的 JSON 提取实现（双方言一致）。
不追求完整 HR：只做「老板算得清、发得出、有留痕」的核算单。
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.canonical import ENTITY_TYPES, CanonicalRecord
from app.models.ops import PayrollRule

logger = get_logger(__name__)

_MODES = ("piece", "hourly", "commission", "bonus")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象")
    mode = str(spec.get("mode") or "").strip()
    if mode not in _MODES:
        raise ValueError(f"mode 须为 {'/'.join(_MODES)}")
    entity = str(spec.get("entity") or "").strip()
    if entity not in ENTITY_TYPES:
        raise ValueError(f"entity 须为 {'/'.join(ENTITY_TYPES)}")
    worker_field = str(spec.get("worker_field") or "").strip()
    if not worker_field or "." not in worker_field:
        raise ValueError("worker_field 必须是 JSON 路径（如 payload.worker）")
    if mode in ("piece", "hourly") and not spec.get("unit_field"):
        raise ValueError(f"{mode} 需要 unit_field（件数/小时数路径）")
    if mode == "commission" and not spec.get("amount_field"):
        raise ValueError("commission 需要 amount_field（提成基数路径）")
    try:
        float(spec.get("rate", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rate 须为数字：{spec.get('rate')}") from exc
    filters = spec.get("filters") or []
    if not isinstance(filters, list):
        raise ValueError("filters 必须是数组")
    return {"mode": mode, "entity": entity, "worker_field": worker_field,
            "unit_field": spec.get("unit_field"),
            "amount_field": spec.get("amount_field"),
            "rate": float(spec.get("rate") or 0),
            "rate_field": spec.get("rate_field"),
            "filters": filters, "bonus_fixed": float(spec.get("bonus_fixed") or 0)}


def _to_numeric(db: AsyncSession, expr):
    from sqlalchemy import Float, Numeric, cast as sa_cast
    if _dialect(db) == "sqlite":
        return sa_cast(expr, Float)
    return sa_cast(expr, Numeric)


def _dialect(db: AsyncSession) -> str:
    return db.bind.dialect.name if db.bind is not None else "sqlite"


def _json_expr(db: AsyncSession, column, path: str):
    """复用指标层的方言分支（payload/custom_data 根）。"""
    root, _, keys = path.partition(".")
    if root not in ("payload", "custom_data"):
        raise ValueError(f"字段路径根只允许 payload/custom_data：{path}")
    if not keys:
        raise ValueError(f"字段路径非法：{path}")
    if _dialect(db) == "sqlite":
        return func.json_extract(column, "$." + keys)
    if "." not in keys:
        return column.op("->>")(keys)
    return column.op("#>>")(f"{{{keys.replace('.', ',')}}}")


def _conditions(db: AsyncSession, company_id: str, spec: dict) -> list:
    conditions = [CanonicalRecord.company_id == company_id,
                  CanonicalRecord.entity_type == spec["entity"]]
    for f in spec.get("filters") or []:
        expr = _json_expr(db, CanonicalRecord.payload
                          if str(f.get("field", "")).startswith("payload")
                          else CanonicalRecord.custom_data,
                          str(f.get("field")))
        op, value = f.get("op"), f.get("value")
        if op == "eq":
            conditions.append(expr == value)
        elif op == "gte":
            conditions.append(_to_numeric(db, expr) >= value)
        elif op == "lte":
            conditions.append(_to_numeric(db, expr) <= value)
        elif op == "in":
            conditions.append(expr.in_(list(value or [])))
        else:
            raise ValueError(f"过滤 op 须为 eq/gte/lte/in：{op}")
    return conditions


async def evaluate_payroll(db: AsyncSession, company_id: str,
                           rule_key: str) -> dict:
    """核算一个规则：按 worker 聚合 → 金额明细单。"""
    row = (await db.execute(
        select(PayrollRule).where(PayrollRule.company_id == company_id,
                                  PayrollRule.key == rule_key))).scalars().first()
    if row is None:
        raise LookupError(f"工资规则 {rule_key} 不存在")
    if not row.active:
        raise ValueError(f"工资规则 {rule_key} 已停用")
    spec = validate_spec(dict(row.spec or {}))

    worker_expr = _json_expr(db, CanonicalRecord.payload, spec["worker_field"])
    conditions = _conditions(db, company_id, spec)

    if spec["mode"] in ("piece", "hourly"):
        unit_expr = _to_numeric(db, _json_expr(db, CanonicalRecord.payload,
                                               spec["unit_field"]))
        rows = (await db.execute(
            select(worker_expr, func.sum(unit_expr))
            .where(*conditions).group_by(worker_expr))).all()
        details = {str(w if w is not None else "(空)"):
                   {"units": _num(u), "amount": _round(_num(u) * spec["rate"])}
                   for w, u in rows}
    elif spec["mode"] == "commission":
        amount_expr = _to_numeric(db, _json_expr(db, CanonicalRecord.payload,
                                                 spec["amount_field"]))
        rows = (await db.execute(
            select(worker_expr, func.sum(amount_expr))
            .where(*conditions).group_by(worker_expr))).all()
        details = {}
        for w, amt in rows:
            rate = spec["rate"]
            if spec.get("rate_field"):
                # 比例在记录里：取均值口径（同一 worker 的记录比例一致）
                rate_expr = _to_numeric(db, _json_expr(
                    db, CanonicalRecord.payload, spec["rate_field"]))
                avg = (await db.execute(
                    select(func.avg(rate_expr))
                    .where(*conditions, worker_expr == w))).scalar()
                if avg is not None:
                    rate = float(avg)
            details[str(w if w is not None else "(空)")] = {
                "base": _round(_num(amt)),
                "amount": _round(_num(amt) * rate)}
    else:  # bonus
        details = {"(固定)": {"amount": spec["bonus_fixed"]}}

    total = _round(sum((d.get("amount") or 0) for d in details.values()))
    return {"key": rule_key, "name": row.name, "mode": spec["mode"],
            "details": details, "total": total}


async def list_rules(db: AsyncSession, company_id: str) -> list[PayrollRule]:
    return list((await db.execute(
        select(PayrollRule).where(PayrollRule.company_id == company_id)
        .order_by(PayrollRule.key))).scalars().all())


def _num(v):
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _round(v: float):
    return int(v) if float(v).is_integer() else round(float(v), 2)
