"""商业交付运营引擎 API（V6.1）：合规台账 / 工资核算 / 对账 / trace 查询 / 消耗报表。"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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

# ---------------------------------------------------------------- 行业中心

@router.get("/industry-hub")
async def industry_hub(company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """行业中心聚合视图：以安装记录为单位，展示每个行业包注入的全部产物。

    「装了什么、给了什么、哪些还没接线」在这一屏看完——散在六个页面里的
    东西在这里收口（工作流/员工/合规/指标/知识/连接器蓝图）。
    """
    await _owned_guard(db, user, company_id)
    from app.models.canonical import MetricDef
    from app.models.p3_governance import MarketplacePackage, PackageInstallation
    from app.models.workflow import WorkflowDefinition

    rows = (await db.execute(
        select(PackageInstallation, MarketplacePackage)
        .join(MarketplacePackage,
              MarketplacePackage.id == PackageInstallation.package_id)
        .where(PackageInstallation.company_id == company_id,
               MarketplacePackage.package_type == "industry_pack",
               PackageInstallation.rolled_back_at.is_(None))
        .order_by(PackageInstallation.created_at.desc()))).all()

    installed = []
    for inst, pkg in rows:
        manifest = pkg.manifest or {}
        artifacts = (inst.install_log or {}).get("artifacts") or []
        agent_ids = [a["id"] for a in artifacts if a.get("kind") == "agent"]
        wf_ids = [a["id"] for a in artifacts if a.get("kind") == "workflow"]
        metric_ids = [a["id"] for a in artifacts if a.get("kind") == "metric"]
        wf_rows = (await db.execute(
            select(WorkflowDefinition).where(WorkflowDefinition.id.in_(wf_ids))
            .order_by(WorkflowDefinition.created_at))).scalars().all()
        workflows = [{"id": w.id, "name": w.name,
                      "status": str(w.status.value)} for w in wf_rows]
        m_rows = (await db.execute(
            select(MetricDef).where(MetricDef.id.in_(metric_ids))
            .order_by(MetricDef.key))).scalars().all()
        metrics = [{"key": m.key, "name": m.name, "unit": m.unit,
                    "wired": bool(m.spec)} for m in m_rows]
        ledgers = (await db.execute(
            select(ComplianceLedger).where(
                ComplianceLedger.company_id == company_id,
                ComplianceLedger.pack_id == str(pkg.key)))
            ).scalars().all()
        compliance = [compliance_service.ledger_out(l) for l in ledgers]
        installed.append({
            "installation_id": inst.id, "package_id": pkg.id, "key": pkg.key,
            "name": pkg.name, "version": inst.version or pkg.latest_version,
            "installed_at": (inst.created_at.isoformat()
                             if inst.created_at else None),
            "description": manifest.get("description") or pkg.description,
            "agents_count": len(agent_ids),
            "workflows": workflows,
            "draft_workflows": sum(1 for w in workflows
                                   if w["status"] == "DRAFT"),
            "metrics": metrics,
            "metrics_pending": sum(1 for m in metrics if not m["wired"]),
            "compliance": compliance,
            "integration_targets": manifest.get("integration_targets") or [],
        })

    from app.services.industry_pack_service import list_builtins
    published_keys = set(r[1].key for r in rows)
    available = [{"id": p["id"], "name": p["name"], "version": p["version"],
                  "description": p["description"],
                  "already_installed": p["id"] in published_keys}
                 for p in list_builtins()]
    return {"installed": installed, "available": available}


@router.post("/industry-hub/workflows/{wid}/publish")
async def publish_pack_workflow(wid: str, company_id: str = Query(...),
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    """发布一条行业包带来的草稿工作流（老板审完图纸，一键上岗）。"""
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    from app.models.workflow import WorkflowDefinition, WorkflowStatus
    wf = await db.get(WorkflowDefinition, wid)
    if wf is None or wf.company_id != company_id:
        raise HTTPException(404, "工作流不存在")
    if wf.source != "industry_pack":
        raise HTTPException(400, "只支持发布行业包预置的工作流")
    if wf.status != WorkflowStatus.DRAFT:
        return {"ok": True, "workflow_id": wid, "status": str(wf.status.value),
                "message": "已是发布状态"}
    wf.status = WorkflowStatus.PUBLISHED
    await db.commit()
    from app.services.event_system import log_event
    await log_event(db, "workflow.published", "workflow_definition", wid,
                    "user", user.id, {"source": "industry_hub"},
                    company_id=company_id)
    await db.commit()
    return {"ok": True, "workflow_id": wid, "status": "PUBLISHED"}


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


# ---------------------------------------------------------------- 敏感访问

async def _log_sensitive_access(db, user, category, target_id, reason,
                                company_id=None, auth="totp",
                                target_desc=None) -> None:
    from app.models.sensitive_access import SensitiveAccess
    db.add(SensitiveAccess(
        id=str(uuid.uuid4()), company_id=company_id,
        admin_user_id=user.id, category=category, target_id=target_id,
        target_desc=(target_desc or "")[:300], reason=reason,
        auth=auth, created_at=datetime.now(timezone.utc)))
    await db.commit()


def _verify_totp_or_fail(request) -> str:
    """敏感内容查看的第二因子：校验 X-TOTP-Code（动态码或恢复码）。

    返回验证方式（totp/recovery）；失败抛 403。冷却策略复用 TOTP 服务
    （5 次失败 60 秒），防止对 6 位码的暴力枚举。"""
    from app.services import totp_service

    code = (request.headers.get("X-TOTP-Code") or "").strip()
    auth = "recovery" if len(code.replace("-", "").replace(" ", "")) > 6         else "totp"
    try:
        totp_service.verify_code(code)
    except PermissionError as exc:
        raise HTTPException(403, f"二次验证失败：{exc}") from exc
    return auth


def _require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if len(text) < 5:
        raise HTTPException(400, "请填写查看原因（至少 5 个字），"
                                 "该理由将随敏感访问台账留档")
    return text[:2000]


@router.get("/admin/logs/events")
async def admin_log_events(request: Request,
                           company_id: str | None = Query(None),
                           event_type: str | None = Query(None),
                           limit: int = Query(200, le=2000),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """系统事件日志：event_log 全量（支持按类型/公司过滤）。"""
    await _admin_guard(db, user, request)
    from app.models.event import EventLog
    q = select(EventLog)
    if company_id:
        q = q.where(EventLog.company_id == company_id)
    if event_type:
        q = q.where(EventLog.event_type.contains(event_type))
    rows = (await db.execute(
        q.order_by(EventLog.created_at.desc()).limit(limit))).scalars().all()
    return {"events": [{
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "event_type": r.event_type, "entity_type": r.entity_type,
        "entity_id": r.entity_id, "actor_type": r.actor_type,
        "actor_id": r.actor_id, "company_id": r.company_id,
        "payload": r.payload,
    } for r in rows]}


@router.get("/admin/logs/chats")
async def admin_log_chats(request: Request,
                          company_id: str = Query(...),
                          limit: int = Query(50, le=200),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """聊天线程目录（元数据：线程/参与方/条数/时间）——不含内容，无需动态码。"""
    await _admin_guard(db, user, request)
    from sqlalchemy import func as _f

    from app.models.agent_chat import AgentChatMessage
    rows = (await db.execute(
        select(
            AgentChatMessage.agent_id,
            AgentChatMessage.company_id,
            AgentChatMessage.thread_id,
            _f.count().label("msgs"),
            _f.max(AgentChatMessage.created_at).label("last_at"),
        ).where(AgentChatMessage.company_id == company_id)
        .group_by(AgentChatMessage.agent_id, AgentChatMessage.thread_id)
        .order_by(_f.max(AgentChatMessage.created_at).desc())
        .limit(limit))).all()
    return {"threads": [{
        "agent_id": r.agent_id, "company_id": r.company_id,
        "thread_id": r.thread_id, "messages": int(r.msgs),
        "last_at": r.last_at.isoformat() if r.last_at else None,
    } for r in rows]}


@router.get("/admin/logs/chats/{agent_id}/messages")
async def admin_log_chat_messages(request: Request, agent_id: str,
                                  thread_id: str | None = Query(None),
                                  reason: str = Query(...),
                                  db: AsyncSession = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """查看聊天内容（敏感）：必须带查看原因 + 当前 TOTP 动态码。

    每次调用在 sensitive_access 台账落一行——谁、何时、什么理由、
    用动态码还是恢复码验证的，全部留痕。"""
    from app.core.config import settings
    import hmac as _hmac

    if not user.is_superadmin:
        raise HTTPException(403, "需要平台超管身份")
    provided = (request.headers.get("X-Super-Key") or "").strip()
    if not provided or not _hmac.compare_digest(provided, settings.super_key):
        raise HTTPException(403, "超级密钥不匹配")
    reason_text = _require_reason(reason)
    auth = _verify_totp_or_fail(request)

    from app.models.agent_chat import AgentChatMessage
    q = select(AgentChatMessage).where(
        AgentChatMessage.agent_id == agent_id)
    if thread_id:
        q = q.where(AgentChatMessage.thread_id == thread_id)
    rows = (await db.execute(
        q.order_by(AgentChatMessage.created_at).limit(500))).scalars().all()
    await _log_sensitive_access(db, user, "chat_thread", agent_id,
                                reason_text, auth=auth,
                                target_desc=f"{len(rows)} 条消息")
    await db.commit()
    return {"messages": [{
        "role": r.role, "content": r.content,
        "thread_id": r.thread_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows], "audited": True}


@router.get("/admin/logs/sensitive-access")
async def admin_sensitive_access(request: Request,
                                 limit: int = Query(100, le=1000),
                                 db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """敏感访问台账：所有查看行为的留痕记录。"""
    await _admin_guard(db, user, request)
    from app.models.sensitive_access import SensitiveAccess
    rows = (await db.execute(
        select(SensitiveAccess)
        .order_by(SensitiveAccess.created_at.desc()).limit(limit))).scalars().all()
    return {"access": [{
        "id": r.id, "admin_user_id": r.admin_user_id,
        "company_id": r.company_id, "category": r.category,
        "target_id": r.target_id, "target_desc": r.target_desc,
        "reason": r.reason, "auth": r.auth,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


# ================================================================ 超级管理后台
# 门禁 = is_superadmin + X-Super-Key 双因子；每一笔操作都落审计。


def _mint_admin_session(user_id: str, ttl_s: int = 12 * 3600) -> tuple[str, int]:
    """签发管理会话令牌：HMAC(jwt_secret, "admin-session:user:exp")。"""
    import base64 as _b64

    from app.core.config import settings

    exp = int(time.time()) + ttl_s
    payload = f"admin-session:{user_id}:{exp}"
    sig = _b64.urlsafe_b64encode(hmac.new(
        settings.jwt_secret.encode(), payload.encode(),
        hashlib.sha256).digest()).decode().rstrip("=")
    token = f"{exp}:{sig}"
    return token, exp


def _verify_admin_session(token: str) -> str | None:
    """校验管理会话令牌，返回 user_id；非法/过期返回 None。"""
    import base64 as _b64

    from app.core.config import settings

    try:
        exp_s, sig = token.split(":", 1)
        payload = f"admin-session:{exp_s}"
        expect = _b64.urlsafe_b64encode(hmac.new(
            settings.jwt_secret.encode(), payload.encode(),
            hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expect, sig):
            return None
        if int(exp_s) < time.time():
            return None
        return exp_s.split(":")[1]
    except Exception:  # noqa: silent-except, BLE001 — 畸形令牌一律拒绝（返回 None 由门禁转 403）
        return None


async def _admin_guard(db: AsyncSession, user: User, request,
                       require_totp: bool = False) -> None:
    """超级后台门禁：身份（超管）+ 凭据三选一——
    * X-Super-Key（根凭据，知道）
    * X-Admin-Token（unlock 验证后签发的管理会话令牌，12 小时）
    require_totp=True 仅用于 unlock 本身（进台验 TOTP 持有）。"""
    import hmac as _hmac

    from app.core.config import settings
    from app.services import totp_service

    if not user.is_superadmin:
        raise HTTPException(403, "需要平台超管身份")
    admin_token = (request.headers.get("X-Admin-Token") or "").strip()
    if admin_token:
        if _verify_admin_session(admin_token) != user.id:
            raise HTTPException(403, "管理会话已失效，请重新进入")
        if require_totp:
            # 会话令牌不含 TOTP 证明：进台动作必须现验动态码或根凭据
            code = (request.headers.get("X-TOTP-Code") or "").strip()
            try:
                totp_service.verify_code(code)
            except PermissionError as exc:
                raise HTTPException(403, f"二次验证失败：{exc}") from exc
        return
    provided = (request.headers.get("X-Super-Key") or "").strip()
    if not provided or not _hmac.compare_digest(provided, settings.super_key):
        raise HTTPException(403, "需要管理会话令牌或超级密钥")
    if require_totp and totp_service.status()["enrolled"]:
        code = (request.headers.get("X-TOTP-Code") or "").strip()
        try:
            totp_service.verify_code(code)
        except PermissionError as exc:
            raise HTTPException(403, f"二次验证失败：{exc}") from exc


@router.post("/admin/unlock")
async def admin_unlock(body: dict, request: Request,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """进台门禁（用户口径「验证器优先」）：

    * 已绑定验证器 → **只验动态码**（不用每次输超级密钥）；
      恢复码同样可用（一次性）。
    * 未绑定 → 验超级密钥（首次绑定/重新绑定的通道）。
    失败均落审计；动态码有 5 次失败 60 秒冷却。"""
    import hmac as _hmac

    from app.core.config import settings
    from app.services import totp_service

    if not user.is_superadmin:
        raise HTTPException(403, "需要平台超管身份")
    enrolled = totp_service.status()["enrolled"]
    if enrolled:
        code = (request.headers.get("X-TOTP-Code") or "").strip()
        try:
            totp_service.verify_code(code)
        except PermissionError as exc:
            raise HTTPException(403, f"二次验证失败：{exc}") from exc
        via = "totp"
    else:
        provided = (request.headers.get("X-Super-Key") or "").strip()
        if not provided or not _hmac.compare_digest(provided,
                                                    settings.super_key):
            raise HTTPException(403, "超级密钥不匹配")
        via = "super_key"
    admin_token, exp = _mint_admin_session(user.id)
    from app.services.event_system import log_event
    await log_event(db, "admin.unlocked", "user", user.id, "user", user.id,
                    {"via": via, "session_exp": exp}, company_id=None)
    await db.commit()
    return {"ok": True, "message": "超级管理后台已解锁", "via": via,
            "admin_token": admin_token, "expires_at": exp}


@router.post("/admin/2fa/enroll")
async def admin_2fa_enroll(body: dict, request: Request,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """生成 TOTP 绑定二维码（根凭据=超级密钥；旧绑定立即作废）。"""
    await _admin_guard(db, user, request, require_totp=False)
    from app.services import totp_service
    uri = totp_service.enroll(str(body.get("account") or user.email))
    import segno
    qr_data_uri = segno.make(uri).svg_data_uri(scale=5, dark="#1a1d24")
    return {"ok": True, "otpauth_uri": uri, "qr_svg": qr_data_uri,
            "hint": "用 Authenticator 扫码 → 输入 30 秒动态码 confirm 绑定"}


@router.get("/admin/2fa/status")
async def admin_2fa_status(db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """2FA 绑定状态：仅需超管登录态（不带超级密钥）——门禁路由据此选屏。

    只暴露 enrolled 布尔值，不泄露任何密钥材料。"""
    if not user.is_superadmin:
        raise HTTPException(403, "需要平台超管身份")
    from app.services import totp_service
    return {"ok": True, **totp_service.status()}


@router.post("/admin/2fa/confirm")
async def admin_2fa_confirm(body: dict, request: Request,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """首码确认绑定：生效 + 签发一次性恢复码（明文仅出现这一次）。"""
    await _admin_guard(db, user, request, require_totp=False)
    from app.services import totp_service
    try:
        result = totp_service.confirm_enrollment(str(body.get("code") or ""))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.services.event_system import log_event
    await log_event(db, "admin.2fa_enrolled", "user", user.id, "user",
                    user.id, {"via": "authenticator"}, company_id=None)
    await db.commit()
    return {"ok": True, **result}


@router.post("/admin/2fa/disable")
async def admin_2fa_disable(body: dict, request: Request,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """解除 TOTP 绑定（超级密钥根凭据把门；丢手机的部署者也可删文件重绑）。"""
    await _admin_guard(db, user, request, require_totp=False)
    if not body.get("confirm"):
        raise HTTPException(400, "请显式 confirm=true")
    from app.services import totp_service
    totp_service.disable()
    from app.services.event_system import log_event
    await log_event(db, "admin.2fa_disabled", "user", user.id, "user",
                    user.id, {}, company_id=None)
    await db.commit()
    return {"ok": True, "message": "TOTP 绑定已解除（建议立即重新绑定）"}


def totp_status():
    from app.services import totp_service
    return totp_service.status()


@router.get("/admin/overview")
async def admin_overview(request: Request,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """运营总览：全部用户 + 名下公司 + 每家的行业授权与安装状态。"""
    await _admin_guard(db, user, request)
    from app.models.company import Company
    from app.models.p3_governance import MarketplacePackage, PackageInstallation
    from app.models.user import User as UserRow

    users = (await db.execute(
        select(UserRow).order_by(UserRow.created_at))).scalars().all()
    companies = (await db.execute(
        select(Company).order_by(Company.created_at))).scalars().all()
    installs = (await db.execute(
        select(PackageInstallation, MarketplacePackage)
        .join(MarketplacePackage,
              MarketplacePackage.id == PackageInstallation.package_id)
        .where(MarketplacePackage.package_type == "industry_pack",
               PackageInstallation.rolled_back_at.is_(None))
        .order_by(PackageInstallation.created_at.desc()))).all()
    by_company: dict[str, list[dict]] = {}
    for inst, pkg in installs:
        by_company.setdefault(inst.company_id, []).append({
            "key": pkg.key, "name": pkg.name, "version": inst.version})
    from app.models.feature_flags import CompanyFeatureFlag
    flags = (await db.execute(
        select(CompanyFeatureFlag).where(
            CompanyFeatureFlag.enabled.is_(True)))).scalars().all()
    flags_by_company: dict[str, list[str]] = {}
    for f in flags:
        flags_by_company.setdefault(f.company_id, []).append(f.key)

    companies_out = []
    for c in companies:
        owner = next((u for u in users if u.id == c.owner_id), None)
        companies_out.append({
            "id": c.id, "name": c.name, "industry": c.industry,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "owner": {"id": owner.id, "email": owner.email,
                      "username": owner.username} if owner else None,
            "industry_packs": by_company.get(c.id, []),
            "flags": flags_by_company.get(c.id, []),
        })
    return {"users": [{
        "id": u.id, "email": u.email, "username": u.username,
        "is_superadmin": bool(u.is_superadmin),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in users], "companies": companies_out}


class IndustrySetIn(BaseModel):
    industry: str | None = None          # 行业类别（如 manufacturing）
    packs_install: list[str] = Field(default_factory=list)   # 待解锁行业包 id（内置目录 id）
    packs_remove: list[dict] = Field(default_factory=list)   # [{key, uninstall: bool}]


@router.post("/admin/companies/{cid}/entitlements")
async def set_entitlements(cid: str, body: IndustrySetIn,
                           request: Request,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """给一家公司做内容配置：行业类别 + 行业解锁/收回（解锁=自动安装，收回=停用或卸载）。"""
    await _admin_guard(db, user, request)
    from datetime import datetime, timezone

    from app.models.company import Company
    from app.models.feature_flags import CompanyFeatureFlag
    from app.services.event_system import log_event

    from app.models.p3_governance import MarketplacePackage, PackageInstallation
    from app.models.toolbus import ToolRegistry

    company = await db.get(Company, cid)
    if company is None:
        raise HTTPException(404, "公司不存在")
    actions: list[str] = []

    # 1) 行业类别
    if body.industry is not None:
        company.industry = body.industry.strip()[:40] or None
        actions.append(f"行业类别={company.industry or '(清空)'}")

    # 2) 行业解锁：内置包 → 发布（若需要）→ 安装进公司 → 点亮 flag
    for pack_id in body.packs_install:
        from app.services.industry_pack_service import get_builtin
        pack = get_builtin(pack_id)
        if pack is None:
            raise HTTPException(400, f"内置行业包 {pack_id!r} 不存在")
        manifest = {k: pack.get(k) for k in
                    ("id", "name", "version", "description", "terms",
                     "custom_fields", "workflow_templates", "metric_defs",
                     "agent_roster", "kb_seed_topics", "compliance_items",
                     "integration_targets")}
        manifest.update({"category": "industry_pack",
                         "runtime": "declarative"})
        pkg = (await db.execute(
            select(MarketplacePackage).where(
                MarketplacePackage.package_type == "industry_pack",
                MarketplacePackage.key == pack_id))).scalars().first()
        if pkg is None:
            pkg = MarketplacePackage(
                id=str(uuid.uuid4()), package_type="industry_pack",
                key=pack["id"], name=str(pack.get("name")),
                description=pack.get("description"), author="AI-EOS 内置",
                latest_version=str(pack.get("version")),
                permissions_required=["data:canonical:rw"], dependencies=[],
                manifest=manifest, published=True)
            db.add(pkg)
            await db.flush()
        else:
            pkg.manifest = manifest
            pkg.latest_version = str(pack.get("version"))
            await db.flush()
        from app.services.package_installer import install_package
        dup_res = await db.execute(
            select(PackageInstallation).where(
                PackageInstallation.company_id == cid,
                PackageInstallation.package_id == pkg.id,
                PackageInstallation.rolled_back_at.is_(None)))
        already = dup_res.scalars().first()
        if already is None:
            result = await install_package(db, pkg, cid, user.id)
            actions.append(
                f"解锁 {pack['name']}（{len(result['artifacts'])} 项产物）")
        else:
            actions.append(f"{pack['name']} 已在用（跳过重复安装）")
        flag_res = await db.execute(
            select(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == cid,
                CompanyFeatureFlag.key == f"industry:{pack_id}"))
        flag = flag_res.scalars().first()
        if flag is None:
            db.add(CompanyFeatureFlag(
                id=str(uuid.uuid4()), company_id=cid,
                key=f"industry:{pack_id}", value={"version": pack.get("version")},
                enabled=True, updated_by=user.id,
                enabled_at=datetime.now(timezone.utc)))
        else:
            flag.enabled = True
            flag.updated_by = user.id

    # 3) 行业收回：flag 停用；uninstall=True 时连产物一起回收
    from sqlalchemy import delete as _delete
    for item in body.packs_remove:
        key = str(item.get("key") or "")
        uninstall = bool(item.get("uninstall"))
        if not key:
            continue
        await db.execute(_delete(CompanyFeatureFlag).where(
            CompanyFeatureFlag.company_id == cid,
            CompanyFeatureFlag.key == f"industry:{key}"))
        if uninstall:
            pkg = (await db.execute(
                select(MarketplacePackage).where(
                    MarketplacePackage.package_type == "industry_pack",
                    MarketplacePackage.key == key))).scalars().first()
            if pkg is None:
                continue
            insts_res = await db.execute(
                select(PackageInstallation).where(
                    PackageInstallation.company_id == cid,
                    PackageInstallation.package_id == pkg.id,
                    PackageInstallation.rolled_back_at.is_(None)))
            insts = insts_res.scalars().all()
            from app.services.package_installer import uninstall_package
            for inst in insts:
                await uninstall_package(db, inst)
            actions.append(f"收回 {key}（含卸载）")
        else:
            actions.append(f"停用 {key}（保留数据）")

    await db.commit()
    await log_event(db, "admin.entitlements_changed", "company", cid,
                    "user", user.id, {"actions": actions}, company_id=cid)
    await db.commit()
    return {"ok": True, "actions": actions}


@router.get("/admin/companies/{cid}")
async def admin_company_detail(cid: str, request: Request,
                               db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """单公司授权明细：行业类别/已装包/插件工具状态/合规台账/指标。"""
    await _admin_guard(db, user, request)
    from app.models.company import Company
    from app.models.canonical import MetricDef
    from app.models.p3_governance import MarketplacePackage, PackageInstallation
    from app.models.toolbus import ToolRegistry

    company = await db.get(Company, cid)
    if company is None:
        raise HTTPException(404, "公司不存在")
    packs = (await db.execute(
        select(PackageInstallation, MarketplacePackage)
        .join(MarketplacePackage,
              MarketplacePackage.id == PackageInstallation.package_id)
        .where(PackageInstallation.company_id == cid,
               MarketplacePackage.package_type == "industry_pack",
               PackageInstallation.rolled_back_at.is_(None)))).all()
    tools = (await db.execute(
        select(ToolRegistry).where(ToolRegistry.company_id == cid)
        .order_by(ToolRegistry.name))).scalars().all()
    ledgers = (await db.execute(
        select(ComplianceLedger).where(
            ComplianceLedger.company_id == cid)
        .order_by(ComplianceLedger.key))).scalars().all()
    metrics = (await db.execute(
        select(MetricDef).where(MetricDef.company_id == cid)
        .order_by(MetricDef.key))).scalars().all()
    return {
        "company": {"id": company.id, "name": company.name,
                    "industry": company.industry},
        "industry_packs": [{"key": p.key, "name": p.name,
                            "version": i.version,
                            "installed_at": (i.created_at.isoformat()
                                             if i.created_at else None)}
                           for i, p in packs],
        "tools": [{"id": t.id, "name": t.name, "source": t.source,
                   "enabled": t.enabled} for t in tools],
        "compliance": [compliance_service.ledger_out(l) for l in ledgers],
        "metrics": [{"key": m.key, "name": m.name, "wired": bool(m.spec)}
                    for m in metrics],
    }
