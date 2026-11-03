"""本机操作策略 API：三档审批、白名单、完全控制会话与硬拦截清单。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.user import User
from app.schemas.responses_plugins import (ComputerPolicyOut, HardBlockGroupOut)
from app.services import computer_policy as policy_service

router = APIRouter(prefix="/computer-agent", tags=["computer-agent"])


class PolicyIn(BaseModel):
    mode: str | None = Field(None, pattern="^(default|auto|full)$")
    auto_dirs: list[str] | None = None
    auto_commands: list[str] | None = None
    auto_apps: list[str] | None = None
    notify_on_finish: bool | None = None


class PolicyDecisionIn(BaseModel):
    action_type: str
    params: dict | None = None


class PolicyDecisionOut(BaseModel):
    decision: str
    mode: str
    reason: str
    matched: str | None = None


@router.get("/policy", response_model=ComputerPolicyOut)
async def get_policy(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    row = await policy_service.get_policy_row(db, company_id)
    return policy_service.serialize_policy(row)


@router.put("/policy", response_model=ComputerPolicyOut)
async def update_policy(
    data: PolicyIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    try:
        row = await policy_service.upsert_policy(
            db, company_id, user.id, mode=data.mode,
            auto_dirs=data.auto_dirs, auto_commands=data.auto_commands,
            auto_apps=data.auto_apps, notify_on_finish=data.notify_on_finish)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return policy_service.serialize_policy(row)


@router.post("/full-control", response_model=ComputerPolicyOut)
async def enter_full_control(
    company_id: str = Query(...),
    minutes: int = Query(60, ge=5, le=240),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """开启限时完全控制；到期由执行路径自动降回默认审批。"""
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    row = await policy_service.upsert_policy(
        db, company_id, user.id, mode="full")
    policy_service.start_full_control(row, minutes)
    await db.commit()
    await db.refresh(row)
    return policy_service.serialize_policy(row)


@router.post("/exit-full-control", response_model=ComputerPolicyOut)
async def leave_full_control(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    row = await policy_service.upsert_policy(db, company_id, user.id)
    policy_service.exit_full_control(row)
    await db.commit()
    await db.refresh(row)
    return policy_service.serialize_policy(row)


@router.get("/hard-blocks", response_model=list[HardBlockGroupOut])
async def list_hard_blocks(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """硬拦截清单：只读展示，任何审批模式都无法关闭。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    return policy_service.hard_blocks()


@router.post("/evaluate", response_model=PolicyDecisionOut)
async def evaluate_action(
    data: PolicyDecisionIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """预演一次操作会被如何处置（不执行任何动作）。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    row = await policy_service.get_policy_row(db, company_id)
    return policy_service.evaluate_action(row, data.action_type, data.params)
