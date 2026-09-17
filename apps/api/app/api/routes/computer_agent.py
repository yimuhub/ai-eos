"""Computer Agent API (P2-14) — 真实本机执行版。

screenshot/type_text/click/key/run_app/run_shell 全部走 macOS 原生工具
（screencapture + osascript + open）真实执行；授权与审计模型不变。
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.computer_agent import ComputerActionLog, ComputerPermission
from app.computer_real import REAL_EXECUTORS, EXTENDED_CAPABILITIES
from app.models.user import User

router = APIRouter(prefix="/computer-agent", tags=["computer-agent"])


class ActionIn(BaseModel):
    action_type: str
    params: dict | None = None


@router.post("/grant")
async def grant_capability(
    capability: str = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户授权某项本机操作能力。"""
    if capability not in EXTENDED_CAPABILITIES:
        raise HTTPException(400,
                            f"capability must be one of {EXTENDED_CAPABILITIES}")
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
    return {"capability": capability, "granted": True}


@router.post("/revoke")
async def revoke_capability(
    capability: str = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id,
        ComputerPermission.capability == capability))
    perm = q.scalars().first()
    if perm:
        perm.granted = False
        await db.commit()
    return {"capability": capability, "granted": False}


@router.get("/permissions")
async def list_permissions(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id))
    perms = {p.capability: p.granted for p in q.scalars().all()}
    return {c: perms.get(c, False) for c in EXTENDED_CAPABILITIES}


@router.post("/execute")
async def execute_action(
    data: ActionIn,
    company_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 请求执行本机操作：检查授权 → 真实执行 → 审计落库。"""
    if data.action_type not in EXTENDED_CAPABILITIES:
        raise HTTPException(400,
                            f"unknown action {data.action_type}")

    q = await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id,
        ComputerPermission.capability == data.action_type))
    perm = q.scalars().first()
    allowed = bool(perm and perm.granted)

    log = ComputerActionLog(
        id=str(uuid.uuid4()), company_id=company_id, agent_id=agent_id,
        action_type=data.action_type, params=data.params, allowed=allowed,
        mock=False)
    if not allowed:
        log.deny_reason = f"capability '{data.action_type}' not granted"
        db.add(log)
        await db.commit()
        raise HTTPException(403, log.deny_reason)

    executor = REAL_EXECUTORS[data.action_type]
    try:
        result = await executor(data.params or {})
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    log.result = result
    db.add(log)
    await db.commit()
    return {"log_id": log.id, "action": data.action_type,
            "result": log.result,
            "mode": "REAL" if result.get("ok") else "REAL_FAILED"}


@router.get("/logs")
async def list_logs(
    company_id: str = Query(...),
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(ComputerActionLog).where(
        ComputerActionLog.company_id == company_id).order_by(
        ComputerActionLog.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    return [{"id": l.id, "agent_id": l.agent_id, "action": l.action_type,
             "allowed": l.allowed, "deny_reason": l.deny_reason,
             "mock": l.mock, "result": l.result,
             "created_at": l.created_at.isoformat()} for l in rows]


@router.get("/screenshots/latest")
async def latest_screenshot(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回最近一张真实截屏的 base64（供前端 <img> 预览）。"""
    import base64
    from pathlib import Path

    q = await db.execute(select(ComputerActionLog).where(
        ComputerActionLog.company_id == company_id,
        ComputerActionLog.action_type == "screenshot",
        ComputerActionLog.allowed == True).order_by(  # noqa: E712
        ComputerActionLog.created_at.desc()))
    log = q.scalars().first()
    if not log or not (log.result or {}).get("image_path"):
        raise HTTPException(404, "no screenshot yet")
    path = Path(log.result["image_path"])
    if not path.exists():
        raise HTTPException(404, "screenshot file missing")
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"image_base64": f"data:image/png;base64,{b64}",
            "taken_at": log.created_at.isoformat(),
            "path": str(path)}
