"""E2 订阅与试用：自动开试用、重复领取、套餐切换、取消幂等与租户隔离。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.subscription import CompanySubscription, SubscriptionStatus
from app.services import subscription_service

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


async def _get(client, company_id: str, user: dict) -> dict:
    r = await client.get(f"/subscription?company_id={company_id}",
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _count(company_id: str) -> int:
    async with AsyncSessionLocal() as s:
        return int((await s.execute(
            select(func.count(CompanySubscription.id))
            .where(CompanySubscription.company_id == company_id))).scalar_one())


async def test_get_auto_creates_trial_once(client, tenants):
    alice, co = tenants.alice, tenants.alice_co

    body = await _get(client, co["id"], alice)
    assert body["plan_code"] == "trial"
    assert body["status"] == "TRIALING"
    assert body["effective_status"] == "TRIALING"
    assert body["seats_limit"] == 10
    assert body["monthly_token_quota"] == 1_000_000
    assert body["limits"] == {"seats": 10, "monthly_tokens": 1_000_000}
    assert body["trial_ends_at"] is not None

    trial_end = datetime.fromisoformat(body["trial_ends_at"])
    # 用「此刻」计算水位：全量套件可能跑几分钟，模块导入时的 NOW 会漂移
    remaining = trial_end - datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(days=13, hours=23) < remaining <= timedelta(days=14, minutes=1)
    assert body["period"]["start"] is None
    assert body["period"]["end"] is None

    # 再次读取不会新建第二条订阅
    again = await _get(client, co["id"], alice)
    assert again["trial_ends_at"] == body["trial_ends_at"]
    assert await _count(co["id"]) == 1


async def test_start_trial_is_one_shot(client, tenants):
    alice, co = tenants.alice, tenants.alice_co

    r = await client.post("/subscription/trial",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "TRIALING"
    assert body["trial_ends_at"] is not None

    # 重复领取 → 409，且不产生第二条订阅
    r = await client.post("/subscription/trial",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 409, r.text
    assert await _count(co["id"]) == 1

    # 已自动开过试用的公司同样拒绝
    bob_co = tenants.bob_co
    await _get(client, bob_co["id"], tenants.bob)
    r = await client.post("/subscription/trial",
                          json={"company_id": bob_co["id"]},
                          headers=tenants.bob["headers"])
    assert r.status_code == 409


async def test_change_plan_updates_limits_and_period(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _get(client, co["id"], alice)

    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "pro"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_code"] == "pro"
    assert body["status"] == "ACTIVE"
    assert body["effective_status"] == "ACTIVE"
    assert body["seats_limit"] == 50
    assert body["monthly_token_quota"] == 20_000_000
    start = datetime.fromisoformat(body["period"]["start"])
    end = datetime.fromisoformat(body["period"]["end"])
    assert end - start == timedelta(days=30)

    # enterprise：不限席位 / 不限量
    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "enterprise"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    ent = r.json()
    assert ent["seats_limit"] is None
    assert ent["monthly_token_quota"] is None

    # 未知套餐 400，且不改变现有套餐
    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "ultra"},
                          headers=alice["headers"])
    assert r.status_code == 400
    assert (await _get(client, co["id"], alice))["plan_code"] == "enterprise"


async def test_cancel_is_idempotent(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _get(client, co["id"], alice)

    first = await client.post("/subscription/cancel",
                              json={"company_id": co["id"]},
                              headers=alice["headers"])
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "CANCELED"
    assert first.json()["effective_status"] == "CANCELED"

    second = await client.post("/subscription/cancel",
                               json={"company_id": co["id"]},
                               headers=alice["headers"])
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CANCELED"
    assert await _count(co["id"]) == 1


async def test_cross_tenant_is_404(client, tenants):
    alice, alice_co = tenants.alice, tenants.alice_co
    bob = tenants.bob

    r = await client.get(f"/subscription?company_id={alice_co['id']}",
                         headers=bob["headers"])
    assert r.status_code == 404
    for path in ("/subscription/trial", "/subscription/cancel"):
        r = await client.post(path, json={"company_id": alice_co["id"]},
                              headers=bob["headers"])
        assert r.status_code == 404, path
    r = await client.post("/subscription/plan",
                          json={"company_id": alice_co["id"], "plan_code": "pro"},
                          headers=bob["headers"])
    assert r.status_code == 404
    # 不存在的公司同样是 404
    r = await client.get("/subscription?company_id=does-not-exist",
                         headers=alice["headers"])
    assert r.status_code == 404


async def test_effective_status_for_expired_trial_and_period(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    async with AsyncSessionLocal() as s:
        sub = CompanySubscription(
            company_id=co["id"], plan_code="trial",
            status=SubscriptionStatus.TRIALING,
            trial_ends_at=NOW - timedelta(days=1),
            seats_limit=10, monthly_token_quota=1_000_000)
        s.add(sub)
        await s.commit()

    body = await _get(client, co["id"], alice)
    # 原始 status 保留，展示态为 TRIAL_EXPIRED
    assert body["status"] == "TRIALING"
    assert body["effective_status"] == "TRIAL_EXPIRED"

    async with AsyncSessionLocal() as s:
        sub = (await s.execute(
            select(CompanySubscription)
            .where(CompanySubscription.company_id == co["id"]))).scalars().first()
        sub.status = SubscriptionStatus.ACTIVE
        sub.trial_ends_at = None
        sub.period_end = NOW - timedelta(hours=1)
        await s.commit()
        assert subscription_service.effective_status(sub, NOW) == "PAST_DUE"
        # 纯函数：不修改落库状态
        assert sub.status is SubscriptionStatus.ACTIVE
