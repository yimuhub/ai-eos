"""订阅 / 试用 / 套餐订单 API（E2）。

* GET  /subscription?company_id=           查看当前订阅（不存在则自动开试用）
* POST /subscription/trial                 领取 14 天试用（每家公司一次，重复 409）
* POST /subscription/plan                  切换套餐：降级自服务；升级必须凭已收款订单
* POST /subscription/cancel                取消订阅（幂等）
* GET  /subscription/orders?company_id=    订单列表（含待确认/已生效）
* POST /subscription/orders                提交升级/续费订单（PENDING）
* POST /subscription/orders/{id}/cancel    取消未收款订单
* POST /subscription/orders/{id}/confirm   平台确认收款（仅 is_superadmin）

读走 VIEW，套餐变更走 ADMIN（公司级配置），订单收款确认是**平台**动作，
不看公司角色——托管的 SaaS 里收款方与客户不是同一批人。

不属于自己的公司一律 404（与全系统一致，不用 403 泄露"公司存在但你没权限"）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.subscription import SubscriptionOrder
from app.models.user import User
from app.schemas.responses import SubscriptionOut
from app.services import subscription_service

router = APIRouter(prefix="/subscription", tags=["subscription"])


class CompanyIn(BaseModel):
    company_id: str


class PlanIn(CompanyIn):
    plan_code: str
    # 已由平台确认收款的订单 id：升级/续费必须带；降级不需要。
    order_id: str | None = None


class OrderIn(CompanyIn):
    plan_code: str
    note: str | None = None


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
    """切换套餐。

    * 带 `order_id`：把一张已收款订单落到订阅上（升级/续费）；
    * 不带：只允许降级，升级/续费会得到 402 并提示先提交订单。
    """
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.ADMIN)
    if data.order_id:
        order = await db.get(SubscriptionOrder, data.order_id)
        if order is None or order.company_id != data.company_id:
            raise HTTPException(404, "订单不存在")
        if order.plan_code != data.plan_code:
            raise HTTPException(400, "订单套餐与请求套餐不一致")
        sub = await subscription_service.apply_order(
            db, data.company_id, data.order_id)
    else:
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
                                    Capability.ADMIN)
    sub = await subscription_service.cancel(db, data.company_id)
    await db.commit()
    return subscription_service.to_public(sub)


@router.get("/orders")
async def list_orders(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = (await db.execute(
        select(SubscriptionOrder)
        .where(SubscriptionOrder.company_id == company_id)
        .order_by(SubscriptionOrder.created_at.desc())
        .limit(50))).scalars().all()
    return {"orders": [subscription_service.order_to_public(o) for o in rows]}


@router.post("/orders", status_code=201)
async def create_order(
    data: OrderIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.ADMIN)
    order = await subscription_service.create_order(
        db, data.company_id, data.plan_code, user.id, note=data.note)
    await db.commit()
    return subscription_service.order_to_public(order)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = await db.get(SubscriptionOrder, order_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    await assert_company_capability(db, user.id, order.company_id,
                                    Capability.ADMIN)
    order = await subscription_service.cancel_order(db, order_id)
    await db.commit()
    return subscription_service.order_to_public(order)


@router.post("/orders/{order_id}/confirm")
async def confirm_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """平台确认收款。**只认 `users.is_superadmin`**——公司管理员无权给自己盖章。

    自托管部署里首位安装者通常同时是超管与公司所有者，因此本地可以自助完成
    「提交订单 → 确认收款」；托管 SaaS 必须由平台运营账号确认，审计里留有
    `confirmed_by`，事后可对账。
    """
    if not user.is_superadmin:
        raise HTTPException(404, "Not found")
    order = await subscription_service.confirm_order(db, order_id, user.id)
    await db.commit()
    return subscription_service.order_to_public(order)
