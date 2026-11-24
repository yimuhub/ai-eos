"""Stripe SDK boundary: authoritative prices, hosted checkout and webhooks."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import stripe
from fastapi import HTTPException

from app.core.config import settings


@dataclass(frozen=True)
class PlanPrice:
    price_id: str
    unit_amount: int
    currency: str


def configured() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_webhook_secret
                and settings.stripe_pro_price_id
                and settings.stripe_enterprise_price_id
                and settings.stripe_success_url and settings.stripe_cancel_url)


def _client() -> stripe.StripeClient:
    return stripe.StripeClient(settings.stripe_secret_key, max_network_retries=0)


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except stripe.StripeError as exc:
        raise HTTPException(503, "支付服务暂时不可用，请稍后重试") from exc


async def retrieve_price(plan_code: str) -> PlanPrice:
    if not configured():
        raise HTTPException(503, "在线支付尚未配置，请使用线下转账或联系平台")
    price_id = (settings.stripe_pro_price_id if plan_code == "pro"
                else settings.stripe_enterprise_price_id
                if plan_code == "enterprise" else "")
    if not price_id:
        raise HTTPException(400, "该套餐不支持在线支付")
    price = await asyncio.to_thread(
        _call, _client().prices.retrieve, price_id)
    if not price.active or price.type != "one_time" or price.recurring:
        raise HTTPException(503, "支付价格配置无效，请联系平台")
    if price.unit_amount is None or not valid_currency(price.currency):
        raise HTTPException(503, "支付价格配置无效，请联系平台")
    return PlanPrice(price.id, price.unit_amount, price.currency.lower())


def valid_currency(value: str) -> bool:
    return len(value) == 3 and value.isascii() and value.isalpha()


def trusted_checkout_url(value: str | None) -> str:
    if not value:
        raise HTTPException(503, "支付服务未返回有效的结账地址")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.hostname not in {"checkout.stripe.com", "buy.stripe.com"}):
        raise HTTPException(503, "支付服务返回了不受信任的结账地址")
    return value


async def create_checkout(*, order_id: str, company_id: str, plan_code: str,
                          price: PlanPrice, operation_id: str) -> dict:
    session = await asyncio.to_thread(
        _call, _client().checkout.sessions.create,
        mode="payment",
        line_items=[{"price": price.price_id, "quantity": 1}],
        expires_at=int(datetime.now(timezone.utc).timestamp()) + 3600,
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
        client_reference_id=order_id,
        metadata={"order_id": order_id, "company_id": company_id,
                  "plan_code": plan_code},
        idempotency_key=f"aieos-{order_id}-{operation_id}",
    )
    if session.livemode != settings.stripe_secret_key.startswith("sk_live_"):
        raise HTTPException(503, "支付环境不匹配，请联系平台")
    return {"id": session.id, "url": trusted_checkout_url(session.url),
            "expires_at": datetime.fromtimestamp(session.expires_at, timezone.utc),
            "payment_intent_id": session.payment_intent,
            "client_reference_id": session.client_reference_id,
            "mode": session.mode, "livemode": session.livemode,
            "metadata": dict(session.metadata or {}),
            "amount_total": session.amount_total, "currency": session.currency,
            "line_items": session.line_items}


async def retrieve_checkout(session_id: str) -> dict:
    return await asyncio.to_thread(
        _call, _client().checkout.sessions.retrieve, session_id,
        expand=["line_items.data.price"])


async def expire_checkout(session_id: str) -> None:
    await asyncio.to_thread(
        _call, _client().checkout.sessions.expire, session_id)


async def retrieve_payment_intent(payment_intent_id: str) -> dict:
    return await asyncio.to_thread(
        _call, _client().payment_intents.retrieve, payment_intent_id)


async def retrieve_charge(charge_id: str) -> dict:
    return await asyncio.to_thread(
        _call, _client().charges.retrieve, charge_id)


def construct_event(payload: bytes, signature: str):
    try:
        return stripe.Webhook.construct_event(
            payload, signature, settings.stripe_webhook_secret)
    except (stripe.SignatureVerificationError, ValueError) as exc:
        raise HTTPException(400, "无效的支付回调签名") from exc
