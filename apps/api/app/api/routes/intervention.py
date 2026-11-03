"""Intervention API (P2-10)."""
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
from app.models.intervention import (ApprovalRequest, InterventionPolicy,
                                     OverrideLog, new_id, requires_approval)
from app.models.user import User
from app.schemas.responses_gate import (ActionCheckOut, ApprovalGateOut,
                                        ApprovalRequestOut, DecideOut,
                                        InterventionPolicyOut, OverrideLogOut,
                                        OverrideOut, WhitelistDeletedOut,
                                        WhitelistOut)

router = APIRouter(prefix="/intervention", tags=["intervention"])


# ---------- 策略 ----------

class PolicyIn(BaseModel):
    level: int                      # 0-4
    project_id: str | None = None
    agent_id: str | None = None
    approval_actions: list[str] | None = None


@router.get("/policy", response_model=InterventionPolicyOut,
            response_model_exclude_unset=True)
async def get_policy(
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询生效策略（项目级优先于公司级，无则返回默认 level=2）。"""
    await assert_company_owned(db, user.id, company_id)
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


@router.put("/policy", response_model=InterventionPolicyOut,
            response_model_exclude_unset=True)
async def set_policy(
    data: PolicyIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not 0 <= data.level <= 4:
        raise HTTPException(400, "level must be 0-4")
    # 归属校验：此前任何登录用户带别人的 company_id 就能改掉该公司的审批档位
    await assert_company_owned(db, user.id, company_id)
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


@router.post("/check", response_model=ActionCheckOut)
async def check_action(
    action_type: str = Query(...),
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 执行动作前调用：是否需要审批。"""
    await assert_company_owned(db, user.id, company_id)
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


@router.post("/requests", response_model=ApprovalGateOut,
             response_model_exclude_unset=True)
async def create_request(
    data: RequestIn,
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
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
    from app.services.notification_service import notify_approval_pending
    await notify_approval_pending(
        db, company_id=company_id,
        title=f"待审批：{data.action_type}",
        body=data.reason or "有新的审批请求等待处理。")
    await db.commit()
    return {"approved": False, "auto": False, "request_id": req.id,
            "status": "PENDING"}


@router.get("/requests", response_model=list[ApprovalRequestOut])
async def list_requests(
    company_id: str = Query(...),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
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
    always_allow: bool = False   # V5.6.1：批准后写入免批白名单（工具级防审批疲劳）


@router.post("/requests/{request_id}/decide", response_model=DecideOut,
             response_model_exclude_unset=True)
async def decide_request(
    request_id: str,
    data: DecideIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    req = await db.get(ApprovalRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    # 归属校验：否则任何登录用户都能代批他人的审批单
    # （含高危本机命令/外部 Agent 自动补执行）
    await assert_company_owned(db, user.id, req.company_id)
    if req.status != "PENDING":
        raise HTTPException(409, f"Already decided: {req.status}")
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve/reject")

    from datetime import datetime as dt, timezone as tz

    new_status = "APPROVED" if data.decision == "approve" else "REJECTED"
    # 结束只读事务：SQLite/WAL 下"读快照 → 升级写"会直接 BUSY。此处无待写数据。
    await db.commit()

    # ---- 抢占裁定权（CAS）----
    # 原实现是"读 status → 改内存 → 跑真实副作用 → 最后 commit"：双击、
    # 前端重试、双端同时点「批准」时两个请求都会通过 PENDING 检查，
    # 于是一条高危本机命令 / 一次对外发送被执行两遍。
    # 现在只有把 PENDING 改成终态的那**一个**请求继续往下走副作用。
    decided_at = dt.now(tz.utc).replace(tzinfo=None)
    res = await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_id,
               ApprovalRequest.status == "PENDING")
        .values(status=new_status, decided_by=user.id,
                decision_note=data.note, decided_at=decided_at)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise HTTPException(409, "该审批单已被处理（并发/重复提交）")

    # V5.6.1：老板批准时勾"始终允许"→ 写免批白名单。
    # 取哪个参数由 computer_service.whitelist_pattern_for 决定：命令类取命令串，
    # 文件读写只取 path——正文每次都不同，取错字段会得到空串，而空串在白名单里
    # 表示「该能力全部放行」，等于批一次就永久放开整个能力。
    # 白名单与裁定在同一个事务里落库：裁定成功而白名单丢失会让下次同一操作被拦。
    if data.decision == "approve" and data.always_allow and req.action_type in (
            "computer_command", "external_agent"):
        from app.models.whitelist import ApprovalWhitelist
        from app.services import computer_service

        payload0 = req.payload or {}
        cap = payload0.get("capability", "external_agent")
        params = payload0.get("params") or (
            {"task_input": payload0.get("task_input", "")}
            if req.action_type == "external_agent" else {})
        pattern = computer_service.whitelist_pattern_for(cap, params)
        if pattern or cap not in computer_service.PATH_SCOPED_CAPABILITIES:
            db.add(ApprovalWhitelist(
                company_id=req.company_id, capability=cap,
                param_pattern=pattern,
                note=(data.note or "老板批准时勾「始终允许」")[:300],
                created_by=user.id))
    # 抢占结果**立即 commit**：之后的真实副作用（本机命令/对外发送）绝不能在
    # 事务里跑，否则慢操作会长期持锁，而且崩溃回滚会让"已裁定"凭空消失。
    await db.commit()
    await db.refresh(req)

    payload = dict(req.payload or {})
    execution_result = None
    awaiting = bool(payload.get("awaiting_execution"))

    # V5.5 P0 / A3：批准挂起的高危本机命令 / 外部 Agent 外发 → 自动补执行。
    # 幂等由第二道 CAS 保证：只有把一个 APPROVED 且待执行的单子推到 EXECUTING
    # 的调用者才真正执行。执行记录（EXECUTING + 结果）先落库再调外部：
    # 进程在外部调用中途崩溃时单子停在 EXECUTING，重试看到非 APPROVED 即跳过
    # ——宁可不做，也不重复做。
    if new_status == "APPROVED" and awaiting and req.action_type in (
            "computer_command", "external_agent"):
        claim = await db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id,
                   ApprovalRequest.status == "APPROVED")
            .values(status="EXECUTING")
            .execution_options(synchronize_session=False))
        if claim.rowcount == 1:
            await db.commit()
            execution_result = await _perform_approved_action(db, req, payload)
            final_payload = dict(payload)
            final_payload["awaiting_execution"] = False
            final_payload["execution"] = execution_result
            await db.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == request_id,
                       ApprovalRequest.status == "EXECUTING")
                .values(status="APPROVED", payload=final_payload)
                .execution_options(synchronize_session=False))
            await db.commit()
            await db.refresh(req)
        else:
            # 别的请求已经/正在执行：返回已有结果，不重复执行副作用
            await db.refresh(req)
            execution_result = (req.payload or {}).get("execution")

    out = {"id": req.id, "status": req.status}
    if execution_result is not None:
        out["execution"] = execution_result
    return out


async def _perform_approved_action(db: AsyncSession, req: ApprovalRequest,
                                   payload: dict) -> dict:
    """执行审批通过后挂起的真实副作用（本机命令 / 外部 Agent 外发）。

    只做"一次"：调用方已通过 EXECUTING CAS 抢占执行权。
    """
    if req.action_type == "computer_command":
        from app.services.computer_service import (
            ComputerActionDenied, execute_computer_action)
        try:
            outcome = await execute_computer_action(
                db, company_id=req.company_id,
                capability=payload.get("capability", ""),
                params=payload.get("params"),
                agent_id=payload.get("agent_id"))
            return {"ok": True, "result": outcome.get("result")}
        except (ComputerActionDenied, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    if req.action_type == "external_agent":
        from app.external_agents import ExternalAgentConnection
        from app.api.routes.external_agents import ExecuteIn, _run_external
        conn = await db.get(ExternalAgentConnection, payload.get("connection_id", ""))
        if conn is None:
            return {"ok": False, "error": "外部连接已删除，无法执行"}
        try:
            edata = ExecuteIn(task_input=payload.get("task_input") or "",
                              config=payload.get("config"))
            out = await _run_external(conn, edata, db)
            if "_error" in out:
                return {"ok": False, "error": out["_error"]}
            out.pop("_code", None)
            return {"ok": True, "result": out}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"unsupported action_type {req.action_type}"}


# ---------- 人工覆盖 ----------

class OverrideIn(BaseModel):
    target_type: str
    target_id: str
    original_decision: dict | None = None
    override_decision: dict | None = None
    reason: str | None = None


@router.post("/override", response_model=OverrideOut)
async def create_override(
    data: OverrideIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    log = OverrideLog(id=new_id(), company_id=company_id,
                      target_type=data.target_type, target_id=data.target_id,
                      original_decision=data.original_decision,
                      override_decision=data.override_decision,
                      reason=data.reason, overridden_by=user.id)
    db.add(log)
    await db.commit()
    return {"id": log.id, "target": f"{data.target_type}:{data.target_id[:8]}"}


@router.get("/overrides", response_model=list[OverrideLogOut])
async def list_overrides(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    rows = (await db.execute(select(OverrideLog).where(
        OverrideLog.company_id == company_id).order_by(
        OverrideLog.created_at.desc()))).scalars().all()
    return [{"id": o.id, "target_type": o.target_type, "target_id": o.target_id,
             "original": o.original_decision, "override": o.override_decision,
             "reason": o.reason, "created_at": o.created_at.isoformat()}
            for o in rows]


# ---------- V5.6.1 工具级免批白名单管理（学 Cline"始终允许"） ----------
@router.get("/whitelist", response_model=list[WhitelistOut])
async def list_whitelist(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """公司当前免批白名单（前端干预中心展示/撤销）。"""
    await assert_company_owned(db, user.id, company_id)
    from app.models.whitelist import ApprovalWhitelist
    rows = (await db.execute(select(ApprovalWhitelist).where(
        ApprovalWhitelist.company_id == company_id)
        .order_by(ApprovalWhitelist.created_at.desc()))).scalars().all()
    return [{
        "id": w.id, "capability": w.capability,
        "param_pattern": w.param_pattern, "note": w.note,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    } for w in rows]


@router.delete("/whitelist/{wl_id}", response_model=WhitelistDeletedOut)
async def delete_whitelist(
    wl_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销一条白名单 → 同类操作立即恢复拦截（老板随时收权）。"""
    from app.models.whitelist import ApprovalWhitelist
    # 归属校验：此前只比对请求参数里的 company_id，调用者带谁的 id
    # 就能删谁的白名单——参数自比对不构成校验
    await assert_company_owned(db, user.id, company_id)
    w = await db.get(ApprovalWhitelist, wl_id)
    if not w or w.company_id != company_id:
        raise HTTPException(404, "whitelist not found")
    await db.delete(w)
    await db.commit()
    return {"id": wl_id, "deleted": True}
