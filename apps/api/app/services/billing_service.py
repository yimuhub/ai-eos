"""Online order checkout, cancellation and verified webhook state transitions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging_config import get_logger
from app.models.subscription import (BillingWebhookEvent, SubscriptionOrder,
                                     SubscriptionOrderStatus)
from app.services import stripe_service, subscription_service

logger = get_logger(__name__)
PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) \
        if value.tzinfo else value


async def begin_checkout(db: AsyncSession, order: SubscriptionOrder) -> dict:
    if order.payment_method != "stripe":
        raise HTTPException(409, "该订单使用线下转账，无需在线结账")
    if not stripe_service.configured():
        raise HTTPException(503, "在线支付尚未配置")
    status = subscription_service._order_status_value(order.status)
    if status != SubscriptionOrderStatus.PENDING.value:
        raise HTTPException(409, f"订单状态为 {status}，不能继续支付")
    if order.checkout_operation_id:
        raise HTTPException(409, "正在创建支付会话，请稍后重试")
    if order.stripe_checkout_session_id:
        current = await stripe_service.retrieve_checkout(order.stripe_checkout_session_id)
        _validate_session(order, current)
        if current.status == "open":
            return {"checkout_url": stripe_service.trusted_checkout_url(current.url),
                    "checkout_expires_at": _iso(current.expires_at)}
        if current.payment_status == "paid":
            raise HTTPException(409, "已收款，正在等待支付回调确认")

    operation_id = str(uuid.uuid4())
    claimed = await db.execute(update(SubscriptionOrder).where(
        SubscriptionOrder.id == order.id,
        SubscriptionOrder.status == SubscriptionOrderStatus.PENDING,
        SubscriptionOrder.checkout_operation_id.is_(None)).values(
            checkout_operation_id=operation_id),
        execution_options={"synchronize_session": False})
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "正在创建支付会话，请稍后重试")
    await db.commit()

    created = None
    try:
        created = await stripe_service.create_checkout(
            order_id=order.id, company_id=order.company_id,
            plan_code=order.plan_code, operation_id=operation_id,
            price=stripe_service.PlanPrice(order.stripe_price_id, order.amount,
                                           order.currency))
        checkout = SimpleNamespace(
            id=created["id"], client_reference_id=created["client_reference_id"],
            mode=created["mode"], livemode=created["livemode"],
            metadata=created["metadata"], amount_total=created["amount_total"],
            currency=created["currency"], line_items=created["line_items"])
        expected = SimpleNamespace(**vars(order))
        expected.stripe_checkout_session_id = created["id"]
        _validate_session(expected, checkout)
        saved = await db.execute(update(SubscriptionOrder).where(
            SubscriptionOrder.id == order.id,
            SubscriptionOrder.status == SubscriptionOrderStatus.PENDING,
            SubscriptionOrder.checkout_operation_id == operation_id).values(
                checkout_operation_id=None,
                stripe_checkout_session_id=created["id"],
                checkout_url=created["url"],
                checkout_expires_at=_naive(created["expires_at"]),
                stripe_payment_intent_id=created["payment_intent_id"],
                payment_livemode=settings.stripe_secret_key.startswith("sk_live_")),
            execution_options={"synchronize_session": False})
        try:
            if saved.rowcount != 1:
                raise HTTPException(409, "订单状态已变化，请刷新后重试")
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(409, "支付会话已被使用，请刷新后重试") from exc
        return {"checkout_url": created["url"],
                "checkout_expires_at": created["expires_at"].isoformat()}
    except Exception:
        await db.rollback()
        if created:
            try:
                await stripe_service.expire_checkout(created["id"])
            except stripe_service.HTTPException as exc:
                logger.warning("未能清理孤立 Stripe Session %s: %s",
                               created["id"], exc.detail)
        await db.execute(update(SubscriptionOrder).where(
            SubscriptionOrder.id == order.id,
            SubscriptionOrder.checkout_operation_id == operation_id).values(
                checkout_operation_id=None),
            execution_options={"synchronize_session": False})
        await db.commit()
        raise


async def cancel_online_order(db: AsyncSession, order: SubscriptionOrder) -> None:
    if order.checkout_operation_id:
        raise HTTPException(409, "正在创建支付会话，请稍后重试")
    if order.stripe_checkout_session_id:
        current = await stripe_service.retrieve_checkout(order.stripe_checkout_session_id)
        if current.status == "open":
            await stripe_service.expire_checkout(order.stripe_checkout_session_id)
        elif current.payment_status == "paid":
            raise HTTPException(409, "订单已收款，正在等待支付回调确认")
    await subscription_service.cancel_order(db, order.id)


def _iso(value: datetime | int | None) -> str | None:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return value.isoformat() if value else None


def _validate_session(order: SubscriptionOrder, session) -> None:
    if (session.id != order.stripe_checkout_session_id
            or session.client_reference_id != order.id
            or session.mode != "payment"
            or session.livemode != settings.stripe_secret_key.startswith("sk_live_")):
        raise HTTPException(400, "支付会话与订单不匹配")
    metadata = dict(session.metadata or {})
    if metadata != {"order_id": order.id, "company_id": order.company_id,
                    "plan_code": order.plan_code}:
        raise HTTPException(400, "支付会话元数据与订单不匹配")
    items = list(session.line_items.data)
    if len(items) != 1 or items[0].price.id != order.stripe_price_id:
        raise HTTPException(400, "支付价格与订单不匹配")
    if (items[0].price.type != "one_time" or items[0].price.recurring
            or items[0].price.unit_amount != order.amount
            or session.amount_total != order.amount
            or session.currency.lower() != order.currency):
        raise HTTPException(400, "支付金额或币种与订单不匹配")


async def _receipt_url(payment_intent_id: str | None) -> str | None:
    if not payment_intent_id:
        return None
    try:
        intent = await stripe_service.retrieve_payment_intent(payment_intent_id)
        charge_id = intent.get("latest_charge")
        if not charge_id:
            return None
        charge = await stripe_service.retrieve_charge(charge_id)
        value = charge.get("receipt_url")
        if value:
            parsed = stripe_service.urlparse(value)
            if parsed.scheme != "https" or parsed.hostname != "receipt.stripe.com":
                return None
        return value
    except HTTPException as exc:
        logger.warning("付款回执查询失败（不影响已确认付款）: %s", exc.detail)
        return None


async def _claim_event(db: AsyncSession, event) -> bool:
    try:
        async with db.begin_nested():
            db.add(BillingWebhookEvent(
                id=event.id, event_type=event.type, livemode=event.livemode,
                event_created_at=datetime.fromtimestamp(event.created, timezone.utc)
                .replace(tzinfo=None), processed_at=subscription_service.utcnow()))
            await db.flush()
        return True
    except IntegrityError:
        return False


async def process_webhook(db: AsyncSession, event) -> None:
    if event.type not in PAID_EVENTS | {"checkout.session.expired"}:
        return
    expected_live = settings.stripe_secret_key.startswith("sk_live_")
    if event.livemode != expected_live:
        raise HTTPException(400, "支付回调环境不匹配")
    session = await stripe_service.retrieve_checkout(event.data.object.id)
    metadata = dict(session.metadata or {})
    order_id = metadata.get("order_id", "")
    order = (await db.execute(select(SubscriptionOrder).where(
        SubscriptionOrder.id == order_id))).scalars().first()
    if order is None or order.payment_method != "stripe":
        raise HTTPException(400, "支付回调找不到在线订单")
    _validate_session(order, session)
    if event.type == "checkout.session.expired":
        if session.status != "expired":
            raise HTTPException(400, "结账会话状态与回调不一致")
        if not await _claim_event(db, event):
            return
        if subscription_service._order_status_value(order.status) \
                == SubscriptionOrderStatus.PENDING.value:
            await subscription_service.cancel_order(db, order.id)
        return
    if session.status != "complete" or session.payment_status != "paid":
        raise HTTPException(400, "支付尚未完成")
    intent_id = session.payment_intent
    if isinstance(intent_id, dict):
        intent_id = intent_id.get("id")
    if intent_id != order.stripe_payment_intent_id:
        raise HTTPException(400, "支付意图与订单不匹配")
    if not await _claim_event(db, event):
        return
    receipt = await _receipt_url(intent_id)
    if receipt:
        order.receipt_url = receipt
        await db.flush()
    await subscription_service.apply_online_paid_order(db, order)
