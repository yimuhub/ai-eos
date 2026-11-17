"""商业交付运营引擎 API（V6.1）：合规台账 / 工资核算 / 对账 / trace 查询 / 消耗报表。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.ops import ComplianceLedger, PayrollRule, ReconciliationRun
from app.models.user import User
from app.services import compliance_service, payroll_service, \
    reconciliation_service
from app.schemas.responses_v61 import (ComplianceCreateOut, ComplianceListOut,
                                        CompliancePatchOut,
                                        ComplianceRefreshOut,
                                        PayrollEvaluateOut, PayrollListOut,
                                        PayrollRuleOut, ReconciliationListOut,
                                        ReconciliationOut, TraceQueryOut,
                                        UsageReportOut)

router = APIRouter(prefix="/ops", tags=["ops"])


async def _owned_guard(db: AsyncSession, user: User, company_id: str,
                 cap: Capability = Capability.VIEW) -> None:
    await assert_company_capability(db, user.id, company_id, cap)


# ---------------------------------------------------------------- 合规台账

class ComplianceIn(BaseModel):
    key: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    authority: str | None = None
    expires_at: str | None = None
    renewal_rule: str | None = None
    renewal_days_before: int = 30
    penalty: str | None = None
    evidence_note: str | None = None


@router.get("/compliance")
async def list_compliance(company_id: str = Query(...),
                          status: str | None = Query(None),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)
                          ) -> ComplianceListOut:
    await _owned_guard(db, user, company_id)
    counts = await compliance_service.refresh_company_status(db, company_id)
    q = select(ComplianceLedger).where(
        ComplianceLedger.company_id == company_id)
    if status:
        q = q.where(ComplianceLedger.status == status)
    rows = (await db.execute(
        q.order_by(ComplianceLedger.key))).scalars().all()
    return {"items": [compliance_service.ledger_out(r) for r in rows],
            "counts": counts}


@router.post("/compliance", status_code=201,
             response_model=ComplianceCreateOut)
async def create_compliance(body: ComplianceIn,
                            company_id: str = Query(...),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    from datetime import datetime

    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at)
        except ValueError as exc:
            raise HTTPException(400, f"expires_at 非法：{body.expires_at}") from exc
    row = ComplianceLedger(
        id=str(uuid.uuid4()), company_id=company_id, key=body.key,
        name=body.name, authority=body.authority, expires_at=expires,
        renewal_rule=body.renewal_rule,
        renewal_days_before=max(1, body.renewal_days_before),
        penalty=body.penalty, evidence_note=body.evidence_note,
        status=compliance_service.derive_status(expires,
                                                body.renewal_days_before),
        created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "item": compliance_service.ledger_out(row)}


@router.patch("/compliance/{item_id}", response_model=CompliancePatchOut)
async def update_compliance(item_id: str, company_id: str = Query(...),
                            expires_at: str | None = Query(None),
                            owner_agent_id: str | None = Query(None),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """更新有效期 / 责任人（续办后把新证照日期录进来）。"""
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    row = await db.get(ComplianceLedger, item_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "台账项不存在")
    if expires_at is not None:
        from datetime import datetime

        try:
            row.expires_at = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise HTTPException(400, f"expires_at 非法：{expires_at}") from exc
    if owner_agent_id is not None:
        row.owner_agent_id = owner_agent_id
    row.status = compliance_service.derive_status(row.expires_at,
                                                  row.renewal_days_before)
    await db.commit()
    return {"ok": True, "item": compliance_service.ledger_out(row)}


@router.post("/compliance/refresh", response_model=ComplianceRefreshOut)
async def refresh_compliance(company_id: str = Query(...),
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    counts = await compliance_service.refresh_company_status(db, company_id)
    return {"ok": True, "counts": counts}


# ---------------------------------------------------------------- 工资核算

class PayrollIn(BaseModel):
    key: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    spec: dict


@router.post("/payroll", status_code=201, response_model=PayrollRuleOut)
async def create_payroll(body: PayrollIn, company_id: str = Query(...),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    try:
        spec = payroll_service.validate_spec(body.spec)
    except ValueError as exc:
        raise HTTPException(400, f"工资口径非法：{exc}") from exc
    dup = (await db.execute(
        select(PayrollRule.id).where(PayrollRule.company_id == company_id,
                                     PayrollRule.key == body.key))).first()
    if dup:
        raise HTTPException(400, f"同名规则已存在：{body.key}")
    row = PayrollRule(id=str(uuid.uuid4()), company_id=company_id,
                      key=body.key, name=body.name, spec=spec,
                      created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "rule": {"id": row.id, "key": row.key,
                                 "name": row.name, "spec": spec}}


@router.get("/payroll", response_model=PayrollListOut)
async def list_payroll(company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id)
    rows = await payroll_service.list_rules(db, company_id)
    return {"rules": [{"id": r.id, "key": r.key, "name": r.name,
                       "mode": (r.spec or {}).get("mode"), "active": r.active,
                       "spec": r.spec} for r in rows]}


@router.post("/payroll/{key}/evaluate", response_model=PayrollEvaluateOut)
async def evaluate_payroll(key: str, company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    try:
        return await payroll_service.evaluate_payroll(db, company_id, key)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------- 对账

class ReconciliationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict


@router.post("/reconciliations", status_code=201,
             response_model=ReconciliationOut)
async def create_reconciliation(body: ReconciliationIn,
                                company_id: str = Query(...),
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    row = ReconciliationRun(id=str(uuid.uuid4()), company_id=company_id,
                            name=body.name, config=body.config,
                            status="running", created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "run": _run_out(row)}


@router.get("/reconciliations", response_model=ReconciliationListOut)
async def list_reconciliations(company_id: str = Query(...),
                               db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id)
    rows = (await db.execute(
        select(ReconciliationRun).where(
            ReconciliationRun.company_id == company_id)
        .order_by(ReconciliationRun.created_at.desc()).limit(50))).scalars().all()
    return {"runs": [_run_out(r) for r in rows]}


@router.post("/reconciliations/{run_id}/run",
             response_model=ReconciliationOut)
async def run_reconciliation_endpoint(run_id: str,
                                      company_id: str = Query(...),
                                      db: AsyncSession = Depends(get_db),
                                      user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    row = await db.get(ReconciliationRun, run_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "对账任务不存在")
    try:
        row = await reconciliation_service.run_reconciliation(db, row)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": row.status == "success", "run": _run_out(row)}


def _run_out(r: ReconciliationRun) -> dict:
    return {"id": r.id, "name": r.name, "config": r.config,
            "status": r.status, "total_a": r.total_a, "total_b": r.total_b,
            "matched": r.matched,
            "only_a": (r.only_a or [])[:20], "only_b": (r.only_b or [])[:20],
            "mismatched": (r.mismatched or [])[:20],
            "error": r.error,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


# ---------------------------------------------------------------- trace 查询

@router.get("/trace/{trace_id}", response_model=TraceQueryOut)
async def trace_query(trace_id: str, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """按 trace_id 串起一次业务动作的全部事件（时间正序）。"""
    await _owned_guard(db, user, company_id)
    from sqlalchemy import func as _func
    from app.models.event import EventLog
    q = (select(EventLog).where(
        EventLog.company_id == company_id,
        _func.json_extract(EventLog.payload, "$.trace_id") == trace_id)
        .order_by(EventLog.created_at))
    rows = (await db.execute(q.limit(500))).scalars().all()
    return {"trace_id": trace_id, "events": [{
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "event_type": e.event_type, "entity_type": e.entity_type,
        "entity_id": e.entity_id, "actor_type": e.actor_type,
        "actor_id": e.actor_id, "payload": e.payload,
    } for e in rows]}


# ---------------------------------------------------------------- 消耗报表

@router.get("/usage-report", response_model=UsageReportOut)
async def usage_report(company_id: str = Query(...),
                       month: str | None = Query(None,
                                                 description="YYYY-MM，缺省当月"),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """按 Agent/场景聚合的 token 消耗报表（计费底座）。"""
    await _owned_guard(db, user, company_id)
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func as _func
    from app.models.token_usage import TokenUsage

    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(400, f"month 须为 YYYY-MM：{month}") from exc
        year = start.year + (1 if start.month == 12 else 0)
        end = start.replace(year=year, month=1 if start.month == 12
                            else start.month + 1)
    else:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now + timedelta(days=1)
    by_agent, by_scenario = {}, {}
    total = {"tokens": 0, "calls": 0, "cost": 0.0}
    rows = (await db.execute(
        select(TokenUsage).where(TokenUsage.company_id == company_id)
        .order_by(TokenUsage.created_at.desc()).limit(20000))).scalars().all()
    for r in rows:
        created = r.created_at
        if created is None:
            continue
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        if not (start <= created < end):
            continue
        tokens = int(r.total_tokens or 0)
        cost = float(r.total_cost or 0)
        total["tokens"] += tokens
        total["calls"] += 1
        total["cost"] = round(total["cost"] + cost, 4)
        if r.agent_id:
            agg = by_agent.setdefault(r.agent_id, {"tokens": 0, "calls": 0,
                                                   "cost": 0.0})
            agg["tokens"] += tokens
            agg["calls"] += 1
            agg["cost"] = round(agg["cost"] + cost, 4)
        sc = r.scenario or "unknown"
        agg2 = by_scenario.setdefault(sc, {"tokens": 0, "calls": 0,
                                           "cost": 0.0})
        agg2["tokens"] += tokens
        agg2["calls"] += 1
        agg2["cost"] = round(agg2["cost"] + cost, 4)
    return {"month": month, "total": total,
            "by_agent": by_agent, "by_scenario": by_scenario}
