"""订阅 / 试用 API（E2）。

* GET  /subscription?company_id=   查看当前订阅（不存在则自动开试用）
* POST /subscription/trial         领取 14 天试用（每家公司一次，重复 409）
* POST /subscription/plan          切换套餐（未知套餐 400）
* POST /subscription/cancel        取消订阅（幂等）

读走 VIEW，写走 MANAGE；不属于自己的公司一律 404（与全系统一致，
不用 403 泄露"公司存在但你没权限"）。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.user import User
from app.schemas.responses import SubscriptionOut
from app.services import subscription_service

router = APIRouter(prefix="/subscription", tags=["subscription"])


class CompanyIn(BaseModel):
    company_id: str


class PlanIn(CompanyIn):
    plan_code: str


@router.get("", response_model=SubscriptionOut)
async def get_subscription(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    sub = await subscription_service.get_or_create(db, company_id)
    await db.commit()
    return subscription_service.to_public(sub)


@router.post("/trial", response_model=SubscriptionOut)
async def start_trial(
    data: CompanyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    sub = await subscription_service.start_trial(db, data.company_id)
    await db.commit()
    return subscription_service.to_public(sub)


@router.post("/plan", response_model=SubscriptionOut)
async def change_plan(
    data: PlanIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    sub = await subscription_service.change_plan(
        db, data.company_id, data.plan_code)
    await db.commit()
    return subscription_service.to_public(sub)


@router.post("/cancel", response_model=SubscriptionOut)
async def cancel_subscription(
    data: CompanyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    sub = await subscription_service.cancel(db, data.company_id)
    await db.commit()
    return subscription_service.to_public(sub)
