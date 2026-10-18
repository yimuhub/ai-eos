"""Intervention API (P2-10)."""
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.intervention import (ApprovalRequest, InterventionPolicy,
                                     OverrideLog, new_id, requires_approval)
from app.models.user import User

router = APIRouter(prefix="/intervention", tags=["intervention"])


# ---------- 策略 ----------

class PolicyIn(BaseModel):
    level: int                      # 0-4
    project_id: str | None = None
    agent_id: str | None = None
    approval_actions: list[str] | None = None


@router.get("/policy")
async def get_policy(
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询生效策略（项目级优先于公司级，无则返回默认 level=2）。"""
    if project_id:
        q = await db.execute(select(InterventionPolicy).where(
            InterventionPolicy.company_id == company_id,
            InterventionPolicy.project_id == project_id))
        p = q.scalars().first()
        if p:
            return _policy_out(p)
    q2 = await db.execute(select(InterventionPolicy).where(
        InterventionPolicy.company_id == company_id,
        InterventionPolicy.project_id.is_(None)))
    p2 = q2.scalars().first()
    if p2:
        return _policy_out(p2)
    return {"company_id": company_id, "project_id": project_id, "level": 2,
            "approval_actions": ["task_create", "task_complete",
                                 "meeting_start", "artifact_approve",
                                 "escalate"],
            "is_default": True}


def _policy_out(p: InterventionPolicy):
    return {"id": p.id, "company_id": p.company_id, "project_id": p.project_id,
            "level": p.level, "approval_actions": p.approval_actions}


@router.put("/policy")
async def set_policy(
    data: PolicyIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not 0 <= data.level <= 4:
        raise HTTPException(400, "level must be 0-4")
    q = await db.execute(select(InterventionPolicy).where(
        InterventionPolicy.company_id == company_id,
        InterventionPolicy.project_id == data.project_id))
    p = q.scalars().first()
    if not p:
        p = InterventionPolicy(id=new_id(), company_id=company_id,
                               project_id=data.project_id)
        db.add(p)
    p.level = data.level
    if data.approval_actions is not None:
        p.approval_actions = data.approval_actions
    p.updated_by = user.id
    await db.commit()
    return _policy_out(p)


@router.post("/check")
async def check_action(
    action_type: str = Query(...),
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 执行动作前调用：是否需要审批。"""
    policy = await _resolve(db, company_id, project_id)
    if policy is None:
        need = requires_approval(2, action_type)
        return {"required": need, "level": 2}
    need = requires_approval(policy.level, action_type, policy.approval_actions)
    return {"required": need, "level": policy.level}


async def _resolve(db, company_id, project_id):
    if project_id:
        q = await db.execute(select(InterventionPolicy).where(
            InterventionPolicy.company_id == company_id,
            InterventionPolicy.project_id == project_id))
        p = q.scalars().first()
        if p:
            return p
    q2 = await db.execute(select(InterventionPolicy).where(
        InterventionPolicy.company_id == company_id,
        InterventionPolicy.project_id.is_(None)))
    return q2.scalars().first()


# ---------- 审批请求 ----------

class RequestIn(BaseModel):
    action_type: str
    payload: dict | None = None
    requested_by_agent_id: str | None = None
    reason: str | None = None
    force: bool = False   # true=策略说不需要也要走审批（Agent 自己拿不准）


@router.post("/requests")
async def create_request(
    data: RequestIn,
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    policy = await _resolve(db, company_id, project_id)
    need = requires_approval((policy.level if policy else 2), data.action_type,
                             policy.approval_actions if policy else None)
    if not need and not data.force:
        return {"approved": True, "auto": True,
                "detail": "策略允许自动执行，无需审批"}

    req = ApprovalRequest(id=new_id(), company_id=company_id,
                          action_type=data.action_type, payload=data.payload,
                          requested_by_agent_id=data.requested_by_agent_id,
                          reason=data.reason or ("force" if data.force and not need else None))
    db.add(req)
    await db.commit()
    return {"approved": False, "auto": False, "request_id": req.id,
            "status": "PENDING"}


@router.get("/requests")
async def list_requests(
    company_id: str = Query(...),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(ApprovalRequest).where(ApprovalRequest.company_id == company_id)
    if status:
        q = q.where(ApprovalRequest.status == status)
    rows = (await db.execute(q.order_by(ApprovalRequest.created_at.desc()))).scalars().all()
    return [{"id": r.id, "action_type": r.action_type, "status": r.status,
             "reason": r.reason, "requested_by": r.requested_by_agent_id,
             "payload": r.payload} for r in rows]


class DecideIn(BaseModel):
    decision: str          # approve / reject
    note: str | None = None


@router.post("/requests/{request_id}/decide")
async def decide_request(
    request_id: str,
    data: DecideIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    req = await db.get(ApprovalRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if req.status != "PENDING":
        raise HTTPException(409, f"Already decided: {req.status}")
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve/reject")
    from datetime import datetime as dt, timezone as tz
    req.status = "APPROVED" if data.decision == "approve" else "REJECTED"
    req.decided_by = user.id
    req.decision_note = data.note
    req.decided_at = dt.now(tz.utc).replace(tzinfo=None)

    # V5.5 P0：批准"高危本机操作"审批单后，自动补执行挂起的高危命令
    execution_result = None
    if req.status == "APPROVED" and req.action_type == "computer_command" \
            and (req.payload or {}).get("awaiting_execution"):
        from app.services.computer_service import (
            ComputerActionDenied, execute_computer_action)
        payload = req.payload or {}
        try:
            outcome = await execute_computer_action(
                db, company_id=req.company_id,
                capability=payload.get("capability", ""),
                params=payload.get("params"),
                agent_id=payload.get("agent_id"))
            execution_result = {"ok": True, "result": outcome.get("result")}
            payload["awaiting_execution"] = False
        except (ComputerActionDenied, ValueError) as exc:
            execution_result = {"ok": False, "error": str(exc)}
        req.payload = payload
    # V5.5 A3：批准「外部 Agent 外发」审批单后，自动执行外发
    elif req.status == "APPROVED" and req.action_type == "external_agent" \
            and (req.payload or {}).get("awaiting_execution"):
        from app.external_agents import ExternalAgentConnection
        from app.api.routes.external_agents import _run_external
        payload = req.payload or {}
        conn = await db.get(ExternalAgentConnection, payload.get("connection_id", ""))
        if conn is None:
            execution_result = {"ok": False, "error": "外部连接已删除，无法执行"}
        else:
            from app.api.routes.external_agents import ExecuteIn
            try:
                edata = ExecuteIn(task_input=payload.get("task_input") or "",
                                   config=payload.get("config"))
                out = await _run_external(conn, edata, db)
                if "_error" in out:
                    execution_result = {"ok": False, "error": out["_error"]}
                else:
                    out.pop("_code", None)
                    execution_result = {"ok": True, "result": out}
            except Exception as exc:
                execution_result = {"ok": False, "error": str(exc)}
        payload["awaiting_execution"] = False
        req.payload = payload
    await db.commit()
    out = {"id": req.id, "status": req.status}
    if execution_result is not None:
        out["execution"] = execution_result
    return out


# ---------- 人工覆盖 ----------

class OverrideIn(BaseModel):
    target_type: str
    target_id: str
    original_decision: dict | None = None
    override_decision: dict | None = None
    reason: str | None = None


@router.post("/override")
async def create_override(
    data: OverrideIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    log = OverrideLog(id=new_id(), company_id=company_id,
                      target_type=data.target_type, target_id=data.target_id,
                      original_decision=data.original_decision,
                      override_decision=data.override_decision,
                      reason=data.reason, overridden_by=user.id)
    db.add(log)
    await db.commit()
    return {"id": log.id, "target": f"{data.target_type}:{data.target_id[:8]}"}


@router.get("/overrides")
async def list_overrides(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(OverrideLog).where(
        OverrideLog.company_id == company_id).order_by(
        OverrideLog.created_at.desc()))).scalars().all()
    return [{"id": o.id, "target_type": o.target_type, "target_id": o.target_id,
             "original": o.original_decision, "override": o.override_decision,
             "reason": o.reason, "created_at": o.created_at.isoformat()}
            for o in rows]
