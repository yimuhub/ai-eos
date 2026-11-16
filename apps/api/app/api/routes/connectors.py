"""连接器与指标 API（V6 M3）。

连接器 = tool_registry 里 source="connector" 的行 + config 里的 kind/映射；
同步账 connector_sync_runs 是监控与排障的事实源。
指标 = metric_defs（结构化口径）+ evaluate（AI 与看板共用一条取数路径）。
自定义字段 = custom_field_defs（企业定制的底座，规整层 custom_data 挂靠）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.canonical import (ENTITY_TYPES, CanonicalRecord,
                                  ConnectorSyncRun, CustomFieldDef, MetricDef)
from app.models.toolbus import ToolRegistry
from app.models.user import User
from app.services.connector_sync import (get_last_sync, run_sync,
                                         sync_run_out)
from app.services.metrics_service import (evaluate_metric, metric_out,
                                          validate_spec)
from app.toolbus.dispatch import _assert_public_http_url

router = APIRouter(prefix="/connectors", tags=["connectors"])

_KINDS = ("rest", "webhook", "mcp_bridge")


async def _guard(db: AsyncSession, user: User, company_id: str,
                 cap: Capability = Capability.VIEW) -> None:
    await assert_company_capability(db, user.id, company_id, cap)


async def _load_connector(db: AsyncSession, company_id: str,
                          tool_id: str) -> ToolRegistry:
    row = await db.get(ToolRegistry, tool_id)
    if row is None or row.company_id != company_id \
            or row.source != "connector":
        raise HTTPException(404, "连接器不存在")
    return row


def _connector_out(row: ToolRegistry, last_sync) -> dict:
    return {"id": row.id, "name": row.name,
            "display_name": row.display_name,
            "description": row.description, "kind": (row.config or {}).get("kind"),
            "entity_type": (row.config or {}).get("entity_type"),
            "config": row.config, "risk": row.risk, "enabled": row.enabled,
            "last_sync": sync_run_out(last_sync) if last_sync else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}


class ConnectorIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    display_name: str | None = None
    description: str | None = None
    kind: str = "rest"
    entity_type: str = "contact"
    config: dict = Field(default_factory=dict)
    auth_secret_ref: str | None = None


@router.post("", status_code=201)
async def create_connector(body: ConnectorIn,
                           company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    await _guard(db, user, company_id, Capability.MANAGE)
    import re as _re

    if body.kind not in _KINDS:
        raise HTTPException(400, f"kind 须为 {'/'.join(_KINDS)}")
    if body.entity_type not in ENTITY_TYPES:
        raise HTTPException(400, f"entity_type 须为 {'/'.join(ENTITY_TYPES)}")
    if not _re.match(r"^[a-z][a-z0-9_-]{2,119}$", body.name or ""):
        raise HTTPException(400, "name 须为小写字母开头的 3~120 位 slug")
    dup = (await db.execute(
        select(ToolRegistry.id).where(
            ToolRegistry.company_id == company_id,
            ToolRegistry.name == body.name))).first()
    if dup:
        raise HTTPException(400, f"同名工具已存在：{body.name}")
    config = dict(body.config or {})
    config["kind"] = body.kind
    config["entity_type"] = body.entity_type
    if body.kind in ("rest", "mcp_bridge"):
        endpoint = str(config.get("endpoint") or "")
        if not endpoint.startswith(("http://", "https://")):
            raise HTTPException(400, "rest/mcp_bridge 连接器需要 endpoint")
        try:
            _assert_public_http_url(endpoint, resolve=False)
        except ValueError as exc:
            raise HTTPException(400, f"endpoint 被拒绝：{exc}") from exc
    row = ToolRegistry(
        id=str(uuid.uuid4()), company_id=company_id, name=body.name,
        display_name=body.display_name or body.name,
        description=body.description, source="connector",
        config=config, risk="medium", required_capability="operate",
        endpoint=config.get("endpoint"),
        auth_secret_ref=body.auth_secret_ref or config.get("auth_secret_ref"),
        created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "connector": _connector_out(row, None)}


@router.get("")
async def list_connectors(company_id: str = Query(...),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    rows = (await db.execute(
        select(ToolRegistry).where(
            ToolRegistry.company_id == company_id,
            ToolRegistry.source == "connector")
        .order_by(ToolRegistry.created_at))).scalars().all()
    out = []
    for row in rows:
        out.append(_connector_out(row, await get_last_sync(db, row.id)))
    return {"connectors": out}


@router.post("/{tool_id}/sync")
async def sync_now(tool_id: str, company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """立即执行一次增量同步（同步账落 connector_sync_runs）。"""
    await _guard(db, user, company_id, Capability.OPERATE)
    row = await _load_connector(db, company_id, tool_id)
    if not row.enabled:
        raise HTTPException(400, "连接器已停用")
    run = await run_sync(db, row, company_id)
    return {"ok": run.status == "success", "run": sync_run_out(run)}


@router.get("/{tool_id}/runs")
async def list_runs(tool_id: str, company_id: str = Query(...),
                    limit: int = Query(20, le=100),
                    db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    await _load_connector(db, company_id, tool_id)
    rows = (await db.execute(
        select(ConnectorSyncRun).where(
            ConnectorSyncRun.company_id == company_id,
            ConnectorSyncRun.tool_id == tool_id)
        .order_by(ConnectorSyncRun.created_at.desc()).limit(limit))).scalars().all()
    return {"runs": [sync_run_out(r) for r in rows]}


@router.post("/{tool_id}/ingest")
async def ingest_records(records: list[dict], tool_id: str,
                         company_id: str = Query(...),
                         signature: str | None = Query(None),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Webhook 收数：外部推送/手工投喂统一入口，幂等 upsert。"""
    await _guard(db, user, company_id, Capability.OPERATE)
    row = await _load_connector(db, company_id, tool_id)
    if (row.config or {}).get("kind") != "webhook":
        raise HTTPException(400, "只有 webhook 连接器支持 ingest")
    from app.connectors.base import upsert_records
    from app.connectors.webhook import WebhookConnector

    import json as _json
    try:
        recs = await WebhookConnector().ingest(
            db, row, _json.dumps(records, ensure_ascii=False).encode("utf-8"),
            signature)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 推送体格式问题转 400
        raise HTTPException(400, f"推送体无法解析：{exc}") from exc
    count = await upsert_records(db, company_id=company_id, source=row.id,
                                 entity_type=str(row.config.get("entity_type")),
                                 records=recs)
    await db.commit()
    return {"ok": True, "upserted": count}


@router.get("/{tool_id}/records")
async def browse_records(tool_id: str, company_id: str = Query(...),
                         limit: int = Query(50, le=200),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    row = await _load_connector(db, company_id, tool_id)
    rows = (await db.execute(
        select(CanonicalRecord).where(
            CanonicalRecord.company_id == company_id,
            CanonicalRecord.source == row.id)
        .order_by(CanonicalRecord.updated_at.desc()).limit(limit))).scalars().all()
    return {"records": [{
        "id": r.id, "entity_type": r.entity_type, "external_id": r.external_id,
        "payload": r.payload, "custom_data": r.custom_data,
        "source_updated_at": (r.source_updated_at.isoformat()
                              if r.source_updated_at else None),
        "seen_at": r.seen_at.isoformat() if r.seen_at else None,
    } for r in rows]}


# ---------------------------------------------------------------- 指标

metrics_router = APIRouter(prefix="/metrics", tags=["metrics"])


class MetricIn(BaseModel):
    key: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    unit: str = ""
    direction: str = "higher_better"
    description: str | None = None
    spec: dict


@metrics_router.post("", status_code=201)
async def create_metric(body: MetricIn, company_id: str = Query(...),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await _guard(db, user, company_id, Capability.MANAGE)
    import re as _re

    if not _re.match(r"^[a-z][a-z0-9_]{1,79}$", body.key or ""):
        raise HTTPException(400, "key 须为小写字母开头的 2~80 位 [a-z0-9_]")
    if body.direction not in ("higher_better", "lower_better"):
        raise HTTPException(400, "direction 须为 higher_better/lower_better")
    try:
        spec = validate_spec(body.spec)
    except ValueError as exc:
        raise HTTPException(400, f"指标口径非法：{exc}") from exc
    dup = (await db.execute(
        select(MetricDef.id).where(MetricDef.company_id == company_id,
                                   MetricDef.key == body.key))).first()
    if dup:
        raise HTTPException(400, f"同名指标已存在：{body.key}")
    row = MetricDef(id=str(uuid.uuid4()), company_id=company_id,
                    key=body.key, name=body.name, unit=body.unit[:20],
                    direction=body.direction, description=body.description,
                    spec=spec, created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "metric": metric_out(row)}


@metrics_router.get("")
async def list_metrics(company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    rows = (await db.execute(
        select(MetricDef).where(MetricDef.company_id == company_id)
        .order_by(MetricDef.key))).scalars().all()
    return {"metrics": [metric_out(r) for r in rows]}


@metrics_router.post("/{key}/evaluate")
async def evaluate(key: str, company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    try:
        return await evaluate_metric(db, company_id, key)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, f"指标口径非法：{exc}") from exc


@metrics_router.delete("/{key}")
async def delete_metric(key: str, company_id: str = Query(...),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await _guard(db, user, company_id, Capability.MANAGE)
    res = await db.execute(delete(MetricDef).where(
        MetricDef.company_id == company_id, MetricDef.key == key))
    await db.commit()
    if not res.rowcount:
        raise HTTPException(404, f"指标 {key} 不存在")
    return {"ok": True, "message": f"已删除指标 {key}"}


# ---------------------------------------------------------------- 自定义字段

custom_fields_router = APIRouter(prefix="/custom-fields",
                                 tags=["custom-fields"])


class CustomFieldIn(BaseModel):
    entity_type: str
    key: str = Field(min_length=2, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    type: str = "text"
    options: list[str] | None = None
    required: bool = False
    description: str | None = None


@custom_fields_router.post("", status_code=201)
async def create_custom_field(body: CustomFieldIn,
                              company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    await _guard(db, user, company_id, Capability.MANAGE)
    import re as _re

    if body.entity_type not in ENTITY_TYPES:
        raise HTTPException(400, f"entity_type 须为 {'/'.join(ENTITY_TYPES)}")
    if not _re.match(r"^[a-z][a-z0-9_]{1,79}$", body.key or ""):
        raise HTTPException(400, "key 须为小写字母开头的 2~80 位 [a-z0-9_]")
    if body.type not in ("text", "number", "date", "select", "boolean"):
        raise HTTPException(400, "type 须为 text/number/date/select/boolean")
    if body.type == "select" and not body.options:
        raise HTTPException(400, "select 字段必须给 options")
    dup = (await db.execute(
        select(CustomFieldDef.id).where(
            CustomFieldDef.company_id == company_id,
            CustomFieldDef.entity_type == body.entity_type,
            CustomFieldDef.key == body.key))).first()
    if dup:
        raise HTTPException(400, f"该实体已有同名字段：{body.key}")
    row = CustomFieldDef(id=str(uuid.uuid4()), company_id=company_id,
                         entity_type=body.entity_type, key=body.key,
                         label=body.label, type=body.type,
                         options=body.options or [], required=body.required,
                         description=body.description, created_by=user.id)
    db.add(row)
    await db.commit()
    return {"ok": True, "field": {
        "id": row.id, "entity_type": row.entity_type, "key": row.key,
        "label": row.label, "type": row.type, "options": row.options,
        "required": row.required, "description": row.description}}


@custom_fields_router.get("")
async def list_custom_fields(company_id: str = Query(...),
                             entity_type: str | None = Query(None),
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    q = select(CustomFieldDef).where(
        CustomFieldDef.company_id == company_id)
    if entity_type:
        q = q.where(CustomFieldDef.entity_type == entity_type)
    rows = (await db.execute(q.order_by(CustomFieldDef.key))).scalars().all()
    return {"fields": [{
        "id": r.id, "entity_type": r.entity_type, "key": r.key,
        "label": r.label, "type": r.type, "options": r.options,
        "required": r.required, "description": r.description} for r in rows]}


@custom_fields_router.delete("/{field_id}")
async def delete_custom_field(field_id: str, company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    await _guard(db, user, company_id, Capability.MANAGE)
    row = await db.get(CustomFieldDef, field_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "字段不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "message": f"已删除字段 {row.key}"}
