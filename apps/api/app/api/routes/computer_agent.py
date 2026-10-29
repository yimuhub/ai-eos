"""Computer Agent API (P2-14) — 真实本机执行版。

screenshot/type_text/click/key/run_app/run_shell 全部走 macOS 原生工具
（screencapture + osascript + open）真实执行；授权与审计模型不变。

改造要点：
1. 全部端点补充 company_id 归属校验——此前任何登录用户只要知道
   company_id 就能替他人公司授权主机操作能力。
2. 执行路径统一走 app/services/computer_service.py，与聊天里的
   computer_* 工具调用共用同一份权限判断与审计逻辑。
"""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    assert_agent_owned,
    assert_company_owned,
    get_current_user,
    get_db,
)
from app.computer_agent import ComputerActionLog, ComputerPermission
from app.computer_real import EXTENDED_CAPABILITIES
from app.core.logging_config import get_logger
from app.models.intervention import ApprovalRequest, InterventionPolicy, new_id
from app.models.user import User
from app.services.computer_service import (
    ComputerActionDenied,
    capability_status,
    execute_computer_action,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/computer-agent", tags=["computer-agent"])


class ActionIn(BaseModel):
    action_type: str
    # V5.7 ⑧ 防呆 ACI：dry_run 只跑限行/参数预检，不真正执行
    dry_run: bool = False
    params: dict | None = None


@router.post("/grant")
async def grant_capability(
    capability: str = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户授权某项本机操作能力。默认全未授权，必须显式开启。"""
    await assert_company_owned(db, user.id, company_id)
    if capability not in EXTENDED_CAPABILITIES:
        raise HTTPException(
            400, f"未知能力 {capability!r}，可用：{', '.join(EXTENDED_CAPABILITIES)}")

    q = await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id,
        ComputerPermission.capability == capability))
    perm = q.scalars().first()
    if not perm:
        perm = ComputerPermission(id=str(uuid.uuid4()),
                                  company_id=company_id, capability=capability)
        db.add(perm)
    perm.granted = True
    perm.granted_by = user.id
    await db.commit()
    logger.info("能力已授权 company=%s capability=%s by=%s",
                company_id, capability, user.id)
    return {"capability": capability, "granted": True}


@router.post("/revoke")
async def revoke_capability(
    capability: str = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    q = await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id,
        ComputerPermission.capability == capability))
    perm = q.scalars().first()
    if perm:
        perm.granted = False
        await db.commit()
    logger.info("能力已撤销 company=%s capability=%s by=%s",
                company_id, capability, user.id)
    return {"capability": capability, "granted": False}


@router.get("/permissions")
async def list_permissions(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """权限总览（复用 service，保证与执行路径使用同一判断）。"""
    await assert_company_owned(db, user.id, company_id)
    return await capability_status(db, company_id)


@router.get("/capabilities")
async def list_capabilities(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """可用能力清单 + 授权状态（前端权限面板用）。"""
    await assert_company_owned(db, user.id, company_id)
    status = await capability_status(db, company_id)
    return {"capabilities": sorted(EXTENDED_CAPABILITIES),
            "granted": status}


# V5.5 P0 审批门：高危本机能力。公司在「干预中心」策略里勾选
# "computer_command"（approval_actions）后，这些操作先建审批单，
# 老板批准后才真正执行（见 intervention /requests/{id}/decide）。
#
# 注意：type_text / click / key 同样是高危能力。它们通过 AppleScript
# 向前台应用注入键鼠事件——先 `open -a Terminal` 再 type_text 就等价于
# 主机命令执行，完全绕开 core/sandbox.py 的 shell 白名单。
HIGH_RISK_CAPABILITIES = {
    "run_shell", "run_write", "run_app",
    "type_text", "click", "key",
}


async def _high_risk_needs_approval(db: AsyncSession, company_id: str) -> bool:
    """公司级策略的 approval_actions 是否含 computer_command（老板主动开启）。"""
    policy = (await db.execute(
        select(InterventionPolicy).where(
            InterventionPolicy.company_id == company_id,
            InterventionPolicy.project_id.is_(None)))).scalars().first()
    return bool(policy and "computer_command" in (policy.approval_actions or []))


@router.post("/execute")
async def execute_action(
    data: ActionIn,
    company_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 请求执行本机操作：审批门 → 检查授权 → 真实执行 → 审计落库。"""
    await assert_company_owned(db, user.id, company_id)

    # Agent 归属校验：此前只要知道 agent_id 就能把它写进审批单 / 审计日志，
    # 可跨公司冒用他人 Agent 的身份发起主机操作。这里同时要求该 Agent
    # 属于当前 company_id，避免「自己的 Agent + 别人的公司」错配。
    if agent_id:
        agent = await assert_agent_owned(db, user.id, agent_id)
        if agent.company_id != company_id:
            raise HTTPException(404, "Agent not found")

    # ---- V5.5 P0：高危操作审批门 ----
    if data.action_type in HIGH_RISK_CAPABILITIES and await _high_risk_needs_approval(db, company_id):
        # V5.6.1 工具级免批白名单（学 Cline"始终允许"防审批疲劳）：
        # 命中白名单 → 直接放行并留审计，不再建审批单
        from app.models.whitelist import ApprovalWhitelist
        wl_rows = (await db.execute(
            select(ApprovalWhitelist).where(
                ApprovalWhitelist.company_id == company_id,
                ApprovalWhitelist.capability == data.action_type)
        )).scalars().all()
        if ApprovalWhitelist.matches(data.action_type, data.params, list(wl_rows)):
            outcome = await execute_computer_action(
                db, company_id=company_id, capability=data.action_type,
                params=data.params, agent_id=agent_id,
                auto_approved="whitelist")
            return {"log_id": outcome["log_id"], "action": outcome["capability"],
                    "result": outcome["result"], "mode": "REAL",
                    "auto_approved": "whitelist",
                    "message": "命中免批白名单，直接放行（已留审计）"}
        req = ApprovalRequest(
            id=new_id(), company_id=company_id,
            action_type="computer_command",
            payload={"capability": data.action_type, "params": data.params,
                     "agent_id": agent_id, "awaiting_execution": True},
            requested_by_agent_id=agent_id,
            reason=f"高危本机操作 {data.action_type}: "
                   f"{json.dumps(data.params, ensure_ascii=False)[:200]}")
        db.add(req)
        await db.commit()
        return {"status": "awaiting_approval", "approval_request_id": req.id,
                "message": "该操作为高危动作，已生成审批单。在「干预中心」批准后自动执行，"
                           "拒绝则丢弃。",
                "mode": "HOLD"}

    # V5.7 ⑧ 防呆 ACI：dry_run 模式——只跑限行/参数预检，不执行
    if data.dry_run:
        from app.services.computer_service import _precheck_capability
        pre = _precheck_capability(data.action_type, data.params or {})
        return {"dry_run": True,
                "result": pre or {"ok": True, "precheck": True,
                                  "message": "预检通过，可真实执行"},
                "precheck_blocked": pre is not None}
    try:
        outcome = await execute_computer_action(
            db, company_id=company_id, capability=data.action_type,
            params=data.params, agent_id=agent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ComputerActionDenied as exc:
        # 未授权：审计日志已在 service 内落库
        raise HTTPException(403, exc.reason) from exc

    result = outcome["result"]
    return {"log_id": outcome["log_id"], "action": outcome["capability"],
            "result": result,
            "mode": "REAL" if result.get("ok") else "REAL_FAILED"}


@router.get("/logs")
async def list_logs(
    company_id: str = Query(...),
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """操作审计日志。所有本机操作（含被拒绝的）都可在前端追溯。"""
    await assert_company_owned(db, user.id, company_id)
    q = await db.execute(select(ComputerActionLog).where(
        ComputerActionLog.company_id == company_id).order_by(
        ComputerActionLog.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    return [{"id": log.id, "agent_id": log.agent_id, "action": log.action_type,
             "allowed": log.allowed, "deny_reason": log.deny_reason,
             "mock": log.mock, "result": log.result,
             "created_at": log.created_at.isoformat()} for log in rows]


@router.get("/screenshots/latest")
async def latest_screenshot(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回最近一张真实截屏的 base64（供前端 <img> 预览）。"""
    import base64

    await assert_company_owned(db, user.id, company_id)
    q = await db.execute(select(ComputerActionLog).where(
        ComputerActionLog.company_id == company_id,
        ComputerActionLog.action_type == "screenshot",
        ComputerActionLog.allowed == True).order_by(  # noqa: E712
        ComputerActionLog.created_at.desc()))
    log = q.scalars().first()
    if not log or not (log.result or {}).get("image_path"):
        raise HTTPException(404, "还没有截屏记录")

    # 截屏路径即便来自数据库也不应无条件信任：仍限制在截屏目录内
    from app.core.sandbox import SandboxViolation, resolve_safe_path

    try:
        path = resolve_safe_path(log.result["image_path"], must_exist=True)
    except SandboxViolation as exc:
        logger.warning("截屏路径越界，拒绝返回：%s", exc)
        raise HTTPException(404, "截屏文件不可访问") from exc

    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"image_base64": f"data:image/png;base64,{b64}",
            "taken_at": log.created_at.isoformat(),
            "path": str(path)}
