"""E2 订阅与试用：自动开试用、重复领取、套餐订单、配额与租户隔离。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.subscription import (CompanySubscription, SubscriptionOrder,
                                     SubscriptionOrderStatus,
                                     SubscriptionStatus)
from app.models.user import User
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


async def _make_superadmin(user_id: str) -> None:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        u.is_superadmin = True
        await s.commit()


async def _request_order(client, company_id: str, plan_code: str,
                         user: dict) -> dict:
    r = await client.post("/subscription/orders",
                          json={"company_id": company_id,
                                "plan_code": plan_code},
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _confirm(client, order_id: str, user: dict) -> dict:
    r = await client.post(f"/subscription/orders/{order_id}/confirm",
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _apply_order(client, company_id: str, plan_code: str,
                       order_id: str, user: dict) -> dict:
    r = await client.post("/subscription/plan",
                          json={"company_id": company_id,
                                "plan_code": plan_code,
                                "order_id": order_id},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _paid_upgrade(client, company_id: str, plan_code: str,
                        user: dict) -> dict:
    order = await _request_order(client, company_id, plan_code, user)
    await _confirm(client, order["id"], user)
    return await _apply_order(client, company_id, plan_code, order["id"], user)


async def test_get_auto_creates_trial_once(client, tenants):
    alice, co = tenants.alice, tenants.alice_co

    body = await _get(client, co["id"], alice)
    assert body["plan_code"] == "trial"
    assert body["status"] == "TRIALING"
    assert body["effective_status"] == "TRIALING"
    assert body["seats_limit"] == 60
    assert body["monthly_token_quota"] == 1_000_000
    assert body["limits"] == {"seats": 60, "monthly_tokens": 1_000_000}
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


async def test_upgrade_without_payment_is_rejected(client, tenants):
    """自助升级洞已堵：没有已收款订单，POST /plan 一律 402 且不改套餐。"""
    alice, co = tenants.alice, tenants.alice_co
    await _get(client, co["id"], alice)

    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "enterprise"},
                          headers=alice["headers"])
    assert r.status_code == 402, r.text
    assert "支付" in r.json()["detail"]

    body = await _get(client, co["id"], alice)
    assert body["plan_code"] == "trial"
    assert body["seats_limit"] == 60


async def test_paid_order_activates_plan(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _make_superadmin(alice["id"])
    await _get(client, co["id"], alice)

    body = await _paid_upgrade(client, co["id"], "pro", alice)
    assert body["plan_code"] == "pro"
    assert body["status"] == "ACTIVE"
    assert body["effective_status"] == "ACTIVE"
    assert body["seats_limit"] == 200
    assert body["monthly_token_quota"] == 20_000_000
    start = datetime.fromisoformat(body["period"]["start"])
    end = datetime.fromisoformat(body["period"]["end"])
    assert end - start == timedelta(days=30)

    # 订单落到 APPLIED，且记录盖章人
    async with AsyncSessionLocal() as s:
        order = (await s.execute(select(SubscriptionOrder))).scalars().one()
        assert order.status is SubscriptionOrderStatus.APPLIED
        assert order.confirmed_by == alice["id"]
        assert order.applied_at is not None


async def test_plan_change_rejects_bad_plan_or_order(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _make_superadmin(alice["id"])
    await _get(client, co["id"], alice)

    # 未知套餐：订单阶段就 400
    r = await client.post("/subscription/orders",
                          json={"company_id": co["id"], "plan_code": "ultra"},
                          headers=alice["headers"])
    assert r.status_code == 400

    # 试用套餐不能当"升级目标"
    r = await client.post("/subscription/orders",
                          json={"company_id": co["id"], "plan_code": "trial"},
                          headers=alice["headers"])
    assert r.status_code == 400

    # 订单套餐与 /plan 请求不一致：400
    order = await _request_order(client, co["id"], "pro", alice)
    await _confirm(client, order["id"], alice)
    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "enterprise",
                                "order_id": order["id"]},
                          headers=alice["headers"])
    assert r.status_code == 400
    assert (await _get(client, co["id"], alice))["plan_code"] == "trial"

    # 不存在的订单：404
    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "pro",
                                "order_id": "no-such-order"},
                          headers=alice["headers"])
    assert r.status_code == 404


async def test_renewal_same_plan_needs_a_new_paid_order(client, tenants):
    """同套餐反复切换 = 白拿周期，必须同样要收款订单。"""
    alice, co = tenants.alice, tenants.alice_co
    await _make_superadmin(alice["id"])
    await _paid_upgrade(client, co["id"], "pro", alice)

    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "pro"},
                          headers=alice["headers"])
    assert r.status_code == 402, r.text

    # 已有待确认订单时重复提交 → 409（避免刷单）
    await _request_order(client, co["id"], "pro", alice)
    r = await client.post("/subscription/orders",
                          json={"company_id": co["id"], "plan_code": "pro"},
                          headers=alice["headers"])
    assert r.status_code == 409


async def test_downgrade_is_self_service_and_keeps_period(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _make_superadmin(alice["id"])
    upgraded = await _paid_upgrade(client, co["id"], "enterprise", alice)
    assert upgraded["seats_limit"] is None

    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "pro"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_code"] == "pro"
    assert body["seats_limit"] == 200
    # 降级不送新周期：周期边界与升级时一致
    assert body["period"]["start"] == upgraded["period"]["start"]
    assert body["period"]["end"] == upgraded["period"]["end"]


async def test_order_confirmation_is_platform_only(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    bob, bob_co = tenants.bob, tenants.bob_co
    await _get(client, co["id"], alice)

    order = await _request_order(client, co["id"], "pro", alice)

    # 公司所有者（非平台超管）不能给自己盖章
    r = await client.post(f"/subscription/orders/{order['id']}/confirm",
                          headers=alice["headers"])
    assert r.status_code == 404, r.text

    # 外部租户也不能确认别家的订单
    r = await client.post(f"/subscription/orders/{order['id']}/confirm",
                          headers=bob["headers"])
    assert r.status_code == 404

    # 未收款订单不能直接生效
    r = await client.post("/subscription/plan",
                          json={"company_id": co["id"], "plan_code": "pro",
                                "order_id": order["id"]},
                          headers=alice["headers"])
    assert r.status_code == 409
    assert "确认收款" in r.json()["detail"]

    # 外部租户看不到、也取消不了别家订单
    r = await client.get(f"/subscription/orders?company_id={co['id']}",
                         headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post(f"/subscription/orders/{order['id']}/cancel",
                          headers=bob["headers"])
    assert r.status_code == 404

    # 待确认订单可以自己取消
    r = await client.post(f"/subscription/orders/{order['id']}/cancel",
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELED"

    # 已收款订单不能取消
    await _make_superadmin(alice["id"])
    paid = await _request_order(client, co["id"], "pro", alice)
    await _confirm(client, paid["id"], alice)
    r = await client.post(f"/subscription/orders/{paid['id']}/cancel",
                          headers=alice["headers"])
    assert r.status_code == 409


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


# ---------------------------------------------------------------------------
# 套餐与账单页（/billing）依赖的展示契约
# ---------------------------------------------------------------------------

async def test_get_subscription_reports_usage(client, tenants):
    """配额进度要能直接渲染：席位与本月 token 都按真实数据回填。"""
    alice, co = tenants.alice, tenants.alice_co
    body = await _get(client, co["id"], alice)
    assert body["plan_label"] == "试用"
    assert body["usage"] == {"seats_used": 0, "tokens_used_this_month": 0}


async def test_plans_catalog_is_free_of_prices(client, tenants):
    """套餐目录只描述能力边界；金额属于商业决策，未确认前不展示。"""
    r = await client.get("/subscription/plans")
    assert r.status_code == 401

    r = await client.get("/subscription/plans", headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    plans = r.json()["plans"]
    assert [p["plan_code"] for p in plans] == ["pro", "enterprise"]
    pro, ent = plans
    assert pro["seats_limit"] == 200
    assert pro["monthly_token_quota"] == 20_000_000
    assert ent["seats_limit"] is None
    assert ent["monthly_token_quota"] is None
    for plan in plans:
        assert not {"price", "amount", "currency", "fee"} & set(plan)


async def test_pending_orders_are_superadmin_only(client, tenants):
    """平台运营视图：非超管一律 404（与全系统"不泄露资源存在"一致）。"""
    alice, co = tenants.alice, tenants.alice_co
    bob = tenants.bob
    await _get(client, co["id"], alice)
    order = await _request_order(client, co["id"], "pro", alice)

    r = await client.get("/subscription/orders/pending", headers=alice["headers"])
    assert r.status_code == 404
    r = await client.get("/subscription/orders/pending", headers=bob["headers"])
    assert r.status_code == 404

    await _make_superadmin(alice["id"])
    r = await client.get("/subscription/orders/pending", headers=alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()["orders"]
    assert len(rows) == 1
    assert rows[0]["id"] == order["id"]
    # 运营要能直接看出是哪家公司在等开通
    assert rows[0]["company_name"] == "A 公司"
    assert rows[0]["status"] == "PENDING"

    await _confirm(client, order["id"], alice)
    r = await client.get("/subscription/orders/pending", headers=alice["headers"])
    assert r.json()["orders"] == []


async def test_me_exposes_superadmin_flag(client, tenants):
    """前端据此决定是否展示平台运营区块；只回布尔，不泄露权限细节。"""
    alice = tenants.alice
    r = await client.get("/auth/me", headers=alice["headers"])
    assert r.status_code == 200
    assert r.json()["is_superadmin"] is False

    await _make_superadmin(alice["id"])
    r = await client.get("/auth/me", headers=alice["headers"])
    assert r.json()["is_superadmin"] is True
