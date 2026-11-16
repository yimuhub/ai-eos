"""指标服务（V6 M3）：结构化口径 → 可复现的数字。

指标 spec（存 metric_defs.spec）：
{
  "entity": "order",                    // canonical entity_type
  "op": "sum",                          // count | sum | avg | min | max
  "field": "payload.amount",            // sum/avg/min/max 必填；count 可空
  "filters": [{"field": "payload.status", "op": "eq", "value": "paid"}],
  "group_by": "payload.region"          // 可选
}

字段路径只支持 `<根>.<键>[.<键>...]`，根 ∈ payload|custom_data；整列
路径（无点）支持 external_id|source|entity_type。实现按方言分支：
SQLite 用 json_extract（JSON1 内置），PG 用 ->>/#>>——同一 spec 两库口径
一致。AI 经 metrics_query 工具消费，**永远不直接打业务库**。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Numeric, Float, func, select, cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.logging_config import get_logger
from app.models.canonical import ENTITY_TYPES, CanonicalRecord, MetricDef

logger = get_logger(__name__)

_ROOTS = ("payload", "custom_data")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_OPS = ("count", "sum", "avg", "min", "max")
_FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"}
_MAX_FILTERS = 20
_COLUMN_FIELDS = ("external_id", "source", "entity_type")


def _dialect(db: AsyncSession) -> str:
    return db.bind.dialect.name if db.bind is not None else "sqlite"


def _json_path_expr(db: AsyncSession, column, field_path: str):
    """字段路径 → SQL 表达式（跨方言）。"""
    parts = field_path.split(".")
    root, keys = parts[0], parts[1:]
    if root not in _ROOTS:
        raise ValueError(f"字段路径根只允许 {'/'.join(_ROOTS)}，"
                         "或整列 external_id/source/entity_type")
    if not keys or any(not _KEY_RE.match(k) for k in keys):
        raise ValueError(f"字段路径非法：{field_path}")
    if _dialect(db) == "sqlite":
        return func.json_extract(column, "$." + ".".join(keys))
    if len(keys) == 1:
        return column.op("->>")(keys[0])
    return column.op("#>>")(f"{{{','.join(keys)}}}")


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象")
    entity = str(spec.get("entity") or "").strip()
    if entity not in ENTITY_TYPES:
        raise ValueError(f"entity 须为规范实体 {'/'.join(ENTITY_TYPES)}，"
                         f"收到 {entity!r}")
    op = str(spec.get("op") or "count")
    if op not in _OPS:
        raise ValueError(f"op 须为 {'/'.join(_OPS)}")
    field_path = spec.get("field")
    if op != "count" and not field_path:
        raise ValueError(f"{op} 需要 field")
    if op == "count":
        field_path = None
    filters = spec.get("filters") or []
    if not isinstance(filters, list) or len(filters) > _MAX_FILTERS:
        raise ValueError(f"filters 须为 ≤{_MAX_FILTERS} 条的数组")
    for f in filters:
        if str(f.get("op")) not in _FILTER_OPS:
            raise ValueError(f"过滤 op 须为 {'/'.join(sorted(_FILTER_OPS))}")
        if not _KEY_RE.match(str(f.get("field") or "")):
            raise ValueError(f"过滤字段路径非法：{f.get('field')}")
    group_by = spec.get("group_by")
    if group_by is not None and not _KEY_RE.match(str(group_by)):
        raise ValueError("group_by 路径非法")
    return {"entity": entity, "op": op, "field": field_path,
            "filters": filters, "group_by": group_by}


def _resolve_field(db: AsyncSession, path: str):
    """路径 → SQL 表达式。"""
    if "." not in path:
        col = {"external_id": CanonicalRecord.external_id,
               "source": CanonicalRecord.source,
               "entity_type": CanonicalRecord.entity_type}.get(path)
        if col is None:
            raise ValueError(f"整列路径只支持 "
                             f"{'/'.join(_COLUMN_FIELDS)}，收到 {path!r}")
        return col
    root = path.split(".", 1)[0]
    if root == "payload":
        return _json_path_expr(db, CanonicalRecord.payload, path)
    if root == "custom_data":
        return _json_path_expr(db, CanonicalRecord.custom_data, path)
    raise ValueError(f"未知字段路径：{path}")


def _to_numeric(db: AsyncSession, expr):
    """JSON 提取结果是文本：聚合前显式转数值（两库口径一致）。"""
    if _dialect(db) == "sqlite":
        return sa_cast(expr, Float)
    return sa_cast(expr, Numeric)


def _build_conditions(db: AsyncSession, spec: dict) -> list:
    conditions = [
        CanonicalRecord.company_id == spec["company_id"],
        CanonicalRecord.entity_type == spec["entity"],
    ]
    for f in spec["filters"]:
        expr = _resolve_field(db, str(f["field"]))
        op, value = f["op"], f.get("value")
        if op == "eq":
            conditions.append(expr == value)
        elif op == "ne":
            conditions.append(expr != value)
        elif op == "gt":
            conditions.append(_to_numeric(db, expr) > value)
        elif op == "gte":
            conditions.append(_to_numeric(db, expr) >= value)
        elif op == "lt":
            conditions.append(_to_numeric(db, expr) < value)
        elif op == "lte":
            conditions.append(_to_numeric(db, expr) <= value)
        elif op == "contains":
            conditions.append(expr.contains(str(value)))
        elif op == "in":
            conditions.append(expr.in_(list(value or [])))
    return conditions


def _agg_expr(db: AsyncSession, op: str, field_path: str | None):
    if op == "count":
        return func.count()
    expr = _to_numeric(db, _resolve_field(db, str(field_path)))
    return {"sum": func.sum, "avg": func.avg,
            "min": func.min, "max": func.max}[op](expr)


def _num(value: Any):
    if value is None:
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else round(f, 4)
    except (TypeError, ValueError):
        return value


async def evaluate_metric(db: AsyncSession, company_id: str, key: str) -> dict:
    """执行一个指标，返回 {value, unit, grouped?, direction}。"""
    row = (await db.execute(
        select(MetricDef).where(MetricDef.company_id == company_id,
                                MetricDef.key == key))).scalars().first()
    if row is None:
        raise LookupError(f"指标 {key} 不存在")
    if not row.spec:
        # 行业包预配的指标：口径已定义、数据源待接线（如实告知，不编数）
        raise LookupError(
            f"指标 {key}（{row.name}）还没有绑定数据源口径："
            "请先安装对应连接器并在指标中心补全 spec（entity/op/field）")
    spec = validate_spec(row.spec)
    spec["company_id"] = company_id
    conditions = _build_conditions(db, spec)
    op = spec["op"]

    base = {"key": key, "name": row.name, "unit": row.unit,
            "direction": row.direction}

    if spec.get("group_by"):
        gexpr = _resolve_field(db, str(spec["group_by"]))
        agg = _agg_expr(db, op, spec.get("field"))
        rows = (await db.execute(
            select(gexpr, agg).where(*conditions).group_by(gexpr))).all()
        grouped = {str(k if k is not None else "(空)"): _num(v)
                   for k, v in rows}
        return {**base, "value": None, "grouped": grouped}

    if op == "count":
        value = (await db.execute(
            select(func.count()).select_from(CanonicalRecord)
            .where(*conditions))).scalar()
        return {**base, "unit": row.unit or "个", "value": int(value or 0),
                "grouped": None}

    agg = _agg_expr(db, op, spec["field"])
    value = (await db.execute(
        select(agg).select_from(CanonicalRecord)
        .where(*conditions))).scalar()
    return {**base, "value": _num(value), "grouped": None}


def metric_out(row: MetricDef) -> dict:
    return {"id": row.id, "key": row.key, "name": row.name, "unit": row.unit,
            "direction": row.direction, "description": row.description,
            "spec": row.spec,
            "required_capability": row.required_capability,
            "created_at": row.created_at.isoformat() if row.created_at else None}
