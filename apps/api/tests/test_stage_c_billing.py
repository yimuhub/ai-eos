"""Stripe billing trust boundary: real signatures, fake provider, temporary DB."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.subscription import (BillingWebhookEvent, CompanySubscription,
                                     SubscriptionOrder)
from app.services import billing_service, stripe_service

pytestmark = pytest.mark.asyncio
WEBHOOK_SECRET = "whsec_stage_c_test"


def price(plan_code="pro"):
    return stripe_service.PlanPrice(
        "price_pro" if plan_code == "pro" else "price_enterprise", 19900, "usd")


def session_for(order, *, status="open", payment_status="unpaid"):
    return SimpleNamespace(
        id=order.stripe_checkout_session_id or "cs_pending",
        client_reference_id=order.id, mode="payment", livemode=False,
        status=status, payment_status=payment_status,
        url="https://checkout.stripe.com/c/pay/cs_pending",
        expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
        payment_intent=order.stripe_payment_intent_id or "pi_pending",
        amount_total=order.amount, currency=order.currency,
        metadata={"order_id": order.id, "company_id": order.company_id,
                  "plan_code": order.plan_code},
        line_items=SimpleNamespace(data=[SimpleNamespace(price=SimpleNamespace(
            id=order.stripe_price_id, type="one_time", recurring=None,
            unit_amount=order.amount))]),
    )


def signed(event_id="evt_1", kind="checkout.session.completed", session_id="cs_pending"):
    payload = json.dumps({
        "id": event_id, "type": kind, "livemode": False,
        "created": int(datetime.now(timezone.utc).timestamp()),
        "data": {"object": {"id": session_id}},
    }, separators=(",", ":")).encode()
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signature = hmac.new(WEBHOOK_SECRET.encode(),
                         timestamp.encode() + b"." + payload,
                         hashlib.sha256).hexdigest()
    return payload, f"t={timestamp},v1={signature}"


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_billing_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "stripe_pro_price_id", "price_pro")
    monkeypatch.setattr(settings, "stripe_enterprise_price_id", "price_enterprise")
    monkeypatch.setattr(settings, "stripe_success_url", "https://example.com/billing")
    monkeypatch.setattr(settings, "stripe_cancel_url", "https://example.com/billing")
    sessions = {}
    created = []
    expired = []

    async def get_price(plan_code):
        return price(plan_code)

    async def create_checkout(**kwargs):
        created.append(kwargs)
        value = {
            "id": f"cs_{len(created)}", "url": f"https://checkout.stripe.com/c/pay/cs_{len(created)}",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "payment_intent_id": f"pi_{len(created)}",
            "status": "open", "payment_status": "unpaid",
            "client_reference_id": kwargs["order_id"], "mode": "payment",
            "livemode": False, "metadata": {
                "order_id": kwargs["order_id"], "company_id": kwargs["company_id"],
                "plan_code": kwargs["plan_code"]},
            "amount_total": kwargs["price"].unit_amount,
            "currency": kwargs["price"].currency,
            "line_items": SimpleNamespace(data=[SimpleNamespace(price=SimpleNamespace(
                id=kwargs["price"].price_id, type="one_time", recurring=None,
                unit_amount=kwargs["price"].unit_amount))]),
        }
        sessions[value["id"]] = value
        return value

    async def retrieve_checkout(session_id):
        if session_id not in sessions:
            raise AssertionError("unknown fake session")
        async with AsyncSessionLocal() as db:
            order = (await db.execute(select(SubscriptionOrder).where(
                SubscriptionOrder.stripe_checkout_session_id == session_id))).scalars().one()
        current = session_for(order, status=sessions[session_id]["status"],
                              payment_status=sessions[session_id]["payment_status"])
        current.id = session_id
        current.url = sessions[session_id]["url"]
        current.expires_at = int(sessions[session_id]["expires_at"].timestamp())
        current.payment_intent = order.stripe_payment_intent_id
        return current

    async def expire_checkout(session_id):
        expired.append(session_id)
        sessions[session_id]["status"] = "expired"

    async def receipt(intent_id):
        return None

    monkeypatch.setattr(stripe_service, "retrieve_price", get_price)
    monkeypatch.setattr(stripe_service, "create_checkout", create_checkout)
    monkeypatch.setattr(stripe_service, "retrieve_checkout", retrieve_checkout)
    monkeypatch.setattr(stripe_service, "expire_checkout", expire_checkout)
    monkeypatch.setattr(billing_service, "_receipt_url", receipt)
    return SimpleNamespace(sessions=sessions, created=created, expired=expired)


async def new_online_order(client, tenants, online):
    response = await client.post("/subscription/orders", headers=tenants.alice["headers"],
                                 json={"company_id": tenants.alice_co["id"],
                                       "plan_code": "pro", "note": None})
    assert response.status_code == 201, response.text
    return response.json()


async def test_authoritative_price_and_client_amount_rejected(client, tenants, online):
    alice, company = tenants.alice, tenants.alice_co
    plans = (await client.get("/subscription/plans", headers=alice["headers"])).json()["plans"]
    assert plans[0]["amount"] == 19900 and plans[0]["currency"] == "usd"
    assert plans[0]["stripe_price_id"] == "price_pro"
    for extra in ({"amount": 1}, {"currency": "cny"}, {"status": "PAID"},
                  {"payment_method": "offline"}):
        response = await client.post("/subscription/orders", headers=alice["headers"],
                                     json={"company_id": company["id"],
                                           "plan_code": "pro", **extra})
        assert response.status_code == 422
    order = await new_online_order(client, tenants, online)
    assert order["payment_method"] == "stripe"
    assert (order["amount"], order["currency"]) == (19900, "usd")


async def test_checkout_lease_reuse_and_manual_actions_blocked(client, tenants, online, monkeypatch):
    order = await new_online_order(client, tenants, online)
    response = await client.post(f"/subscription/orders/{order['id']}/checkout",
                                 headers=tenants.alice["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    assert len(online.created) == 1
    again = await client.post(f"/subscription/orders/{order['id']}/checkout",
                              headers=tenants.alice["headers"])
    assert again.status_code == 200 and len(online.created) == 1

    async def busy(*args, **kwargs):
        raise AssertionError("manual online confirmation must not reach provider")

    monkeypatch.setattr(stripe_service, "retrieve_checkout", busy)
    assert (await client.post(f"/subscription/orders/{order['id']}/confirm",
                              headers=tenants.alice["headers"])).status_code == 404
    assert (await client.post("/subscription/plan", headers=tenants.alice["headers"],
                              json={"company_id": tenants.alice_co["id"],
                                    "plan_code": "pro", "order_id": order["id"]})).status_code == 409
    pending = await client.get("/subscription/orders/pending", headers=tenants.alice["headers"])
    assert pending.status_code == 404


async def test_signed_webhook_applies_once_and_replay_does_not_renew(client, tenants, online,
                                                                    monkeypatch):
    order = await new_online_order(client, tenants, online)
    await client.post(f"/subscription/orders/{order['id']}/checkout",
                      headers=tenants.alice["headers"])
    online.sessions[next(iter(online.sessions))].update(
        status="complete", payment_status="paid")
    payload, signature = signed(session_id=next(iter(online.sessions)))
    response = await client.post("/subscription/webhook", content=payload,
                                 headers={"Stripe-Signature": signature})
    assert response.status_code == 200, response.text
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(BillingWebhookEvent.id))) == 1
        saved = await db.get(SubscriptionOrder, order["id"])
        assert saved.status.value == "APPLIED"
    subscription = (await client.get(
        f"/subscription?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"])).json()
    assert subscription["plan_code"] == "pro" and subscription["status"] == "ACTIVE"
    first_end = subscription["period_end"]
    replay = await client.post("/subscription/webhook", content=payload,
                               headers={"Stripe-Signature": signature})
    assert replay.status_code == 200
    subscription = (await client.get(
        f"/subscription?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"])).json()
    assert subscription["period_end"] == first_end
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(BillingWebhookEvent.id))) == 1
        saved = await db.get(SubscriptionOrder, order["id"])
        assert saved.status.value == "APPLIED"


@pytest.mark.parametrize("mutation", ["amount", "currency", "price", "metadata", "livemode", "unpaid"])
async def test_webhook_rejects_tampered_session(client, tenants, online, monkeypatch, mutation):
    order = await new_online_order(client, tenants, online)
    await client.post(f"/subscription/orders/{order['id']}/checkout",
                      headers=tenants.alice["headers"])
    original = stripe_service.retrieve_checkout

    async def tampered(session_id):
        value = await original(session_id)
        value.status, value.payment_status = "complete", "paid"
        if mutation == "amount":
            value.amount_total = 1
        elif mutation == "currency":
            value.currency = "cny"
        elif mutation == "price":
            value.line_items.data[0].price.id = "price_other"
        elif mutation == "metadata":
            value.metadata = {**value.metadata, "company_id": "other"}
        elif mutation == "livemode":
            value.livemode = True
        elif mutation == "unpaid":
            value.payment_status = "unpaid"
        return value

    monkeypatch.setattr(stripe_service, "retrieve_checkout", tampered)
    payload, signature = signed(session_id=next(iter(online.sessions)))
    response = await client.post("/subscription/webhook", content=payload,
                                 headers={"Stripe-Signature": signature})
    assert response.status_code == 400, response.text
    subscription = (await client.get(
        f"/subscription?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"])).json()
    assert subscription["plan_code"] == "trial"
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(BillingWebhookEvent.id))) == 0


async def test_bad_signature_and_expired_cancel_rebuild(client, tenants, online):
    order = await new_online_order(client, tenants, online)
    payload, _ = signed()
    assert (await client.post("/subscription/webhook", content=payload,
                              headers={"Stripe-Signature": "t=1,v1=bad"})).status_code == 400
    checkout = await client.post(f"/subscription/orders/{order['id']}/checkout",
                                 headers=tenants.alice["headers"])
    assert checkout.status_code == 200
    first_session = next(iter(online.sessions))
    online.sessions[first_session]["status"] = "expired"
    checkout = await client.post(f"/subscription/orders/{order['id']}/checkout",
                                 headers=tenants.alice["headers"])
    assert checkout.status_code == 200 and len(online.created) == 2
    response = await client.post(f"/subscription/orders/{order['id']}/cancel",
                                 headers=tenants.alice["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELED"
    assert online.expired == [next(reversed(online.sessions))]


async def test_receipt_failure_does_not_block_payment(client, tenants, online, monkeypatch):
    order = await new_online_order(client, tenants, online)
    await client.post(f"/subscription/orders/{order['id']}/checkout",
                      headers=tenants.alice["headers"])
    online.sessions[next(iter(online.sessions))].update(status="complete", payment_status="paid")

    async def unavailable(_):
        from fastapi import HTTPException
        raise HTTPException(503, "receipt unavailable")

    monkeypatch.setattr(stripe_service, "retrieve_payment_intent", unavailable)
    payload, signature = signed(session_id=next(iter(online.sessions)))
    response = await client.post("/subscription/webhook", content=payload,
                                 headers={"Stripe-Signature": signature})
    assert response.status_code == 200, response.text
    async with AsyncSessionLocal() as db:
        saved = await db.get(SubscriptionOrder, order["id"])
        assert saved.status.value == "APPLIED" and saved.receipt_url is None


async def test_webhook_event_id_is_durable_not_timestamp(client, tenants, online):
    order = await new_online_order(client, tenants, online)
    await client.post(f"/subscription/orders/{order['id']}/checkout",
                      headers=tenants.alice["headers"])
    online.sessions[next(iter(online.sessions))].update(status="complete", payment_status="paid")
    first, sig1 = signed("evt_duplicate", session_id=next(iter(online.sessions)))
    second, sig2 = signed("evt_distinct", session_id=next(iter(online.sessions)))
    assert (await client.post("/subscription/webhook", content=first,
                              headers={"Stripe-Signature": sig1})).status_code == 200
    assert (await client.post("/subscription/webhook", content=second,
                              headers={"Stripe-Signature": sig2})).status_code == 200
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(BillingWebhookEvent.id))) == 2
        sub = await db.scalar(select(CompanySubscription).where(
            CompanySubscription.company_id == tenants.alice_co["id"]))
        assert sub.plan_code == "pro"


async def test_concurrent_checkout_creation_is_rejected(client, tenants, online, monkeypatch):
    order = await new_online_order(client, tenants, online)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = stripe_service.create_checkout

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(stripe_service, "create_checkout", blocked)
    url = f"/subscription/orders/{order['id']}/checkout"
    first = asyncio.create_task(client.post(url, headers=tenants.alice["headers"]))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = await client.post(url, headers=tenants.alice["headers"])
    assert second.status_code == 409
    release.set()
    assert (await first).status_code == 200
    assert len(online.created) == 1


async def test_checkout_provider_failure_releases_lease(client, tenants, online, monkeypatch):
    from fastapi import HTTPException

    order = await new_online_order(client, tenants, online)
    original = stripe_service.create_checkout

    async def unavailable(**kwargs):
        raise HTTPException(503, "provider unavailable")

    monkeypatch.setattr(stripe_service, "create_checkout", unavailable)
    url = f"/subscription/orders/{order['id']}/checkout"
    assert (await client.post(url, headers=tenants.alice["headers"])).status_code == 503
    async with AsyncSessionLocal() as db:
        saved = await db.get(SubscriptionOrder, order["id"])
        assert saved.checkout_operation_id is None
    monkeypatch.setattr(stripe_service, "create_checkout", original)
    assert (await client.post(url, headers=tenants.alice["headers"])).status_code == 200
    assert len(online.created) == 1


async def test_tampered_created_checkout_is_expired_and_lease_released(
        client, tenants, online, monkeypatch):
    order = await new_online_order(client, tenants, online)
    original = stripe_service.create_checkout

    async def tampered(**kwargs):
        value = await original(**kwargs)
        value["amount_total"] = 1
        return value

    monkeypatch.setattr(stripe_service, "create_checkout", tampered)
    response = await client.post(f"/subscription/orders/{order['id']}/checkout",
                                 headers=tenants.alice["headers"])
    assert response.status_code == 400
    assert len(online.expired) == 1
    async with AsyncSessionLocal() as db:
        saved = await db.get(SubscriptionOrder, order["id"])
        assert saved.checkout_operation_id is None
        assert saved.stripe_checkout_session_id is None
