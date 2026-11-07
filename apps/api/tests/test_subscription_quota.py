"""套餐配额是真的闸门：席位超限拦新增，token 超限拦模型调用。

审查报告实测过的最坏中间态：`seats_limit` / `monthly_token_quota` 只写不读，
客户可以无限扩编、无限烧 token，而 README 把套餐写成了成品。
这里钉住三条语义：

1. 席位上限在新增员工（含组织预设批量导入）前拦截；
2. 试用到期 / 取消且周期结束后冻结（上限归零）；
3. 月度 token 配额在每次 LLM 调用前按流水求和拦截，`None` = 不限。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway, usage_context
from app.models.agent import Agent
from app.models.subscription import CompanySubscription, SubscriptionStatus
from app.models.token_usage import TokenUsage
from app.services import subscription_service

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


async def _set_subscription(company_id: str, **kwargs) -> None:
    defaults = {
        "plan_code": "trial",
        "status": SubscriptionStatus.TRIALING,
        "trial_ends_at": NOW + timedelta(days=7),
        "seats_limit": 60,
        "monthly_token_quota": 1_000_000,
    }
    defaults.update(kwargs)
    async with AsyncSessionLocal() as s:
        s.add(CompanySubscription(company_id=company_id, **defaults))
        await s.commit()


async def _hire(client, user, company_id: str, name: str):
    return await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "分析"},
        headers=user["headers"])


async def _seed_tokens(company_id: str, total: int,
                       created_at: datetime | None = None) -> None:
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(company_id=company_id, scenario="OTHER",
                         input_tokens=total, total_tokens=total,
                         created_at=created_at or NOW))
        await s.commit()


async def test_seat_limit_blocks_hiring(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], seats_limit=1)

    first = await _hire(client, alice, co["id"], "员工一")
    assert first.status_code == 201, first.text

    blocked = await _hire(client, alice, co["id"], "员工二")
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert "席位已满" in detail
    assert "上限 1、已用 1" in detail

    async with AsyncSessionLocal() as s:
        count = (await s.execute(select(func.count()).select_from(Agent))).scalar()
    assert count == 1, "被拦下的员工不能落库"


async def test_expired_trial_freezes_hiring_but_keeps_existing(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], trial_ends_at=NOW - timedelta(days=1),
                            seats_limit=10)
    # 已超编的历史员工保留（不回删）
    async with AsyncSessionLocal() as s:
        for i in range(3):
            s.add(Agent(company_id=co["id"], name=f"老员工{i}",
                        status="IDLE"))
        await s.commit()

    r = await _hire(client, alice, co["id"], "新员工")
    assert r.status_code == 409, r.text
    assert "试用已到期" in r.json()["detail"]

    async with AsyncSessionLocal() as s:
        count = (await s.execute(select(func.count()).select_from(Agent))).scalar()
    assert count == 3


async def test_preset_install_respects_seat_limit(client, tenants):
    """组织预设不能当席位后门：批量导入与逐个点「招人」同一口径。"""
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], seats_limit=2)

    r = await client.post("/org-presets/education/apply",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 409, r.text
    assert "席位已满" in r.json()["detail"]

    async with AsyncSessionLocal() as s:
        assert (await s.execute(
            select(func.count()).select_from(Agent))).scalar() == 0

    # dry_run 只预览，不受拦截（还没落库，不该骗用户"能装"——但它也不写库）
    r = await client.post("/org-presets/education/apply",
                          json={"company_id": co["id"], "dry_run": True},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text


async def test_token_quota_blocks_gateway_call(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], monthly_token_quota=100)
    await _seed_tokens(co["id"], 150)

    async with AsyncSessionLocal() as s:
        gw = ModelGateway(s)
        with usage_context(company_id=co["id"]):
            with pytest.raises(HTTPException) as exc:
                await gw.chat_with_agent_models(
                    ["missing-model"], [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 402
    assert "token 配额已用尽" in exc.value.detail


async def test_token_quota_ignores_previous_months(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], monthly_token_quota=100)
    await _seed_tokens(co["id"], 10_000,
                       created_at=subscription_service.month_start()
                       - timedelta(seconds=1))

    async with AsyncSessionLocal() as s:
        gw = ModelGateway(s)
        with usage_context(company_id=co["id"]):
            # 配额没超 → 走到"模型不存在"，说明闸门放行
            with pytest.raises(RuntimeError) as exc:
                await gw.chat_with_agent_models(
                    ["missing-model"], [{"role": "user", "content": "hi"}])
    assert not isinstance(exc.value, HTTPException)


async def test_token_quota_is_frozen_after_trial_expiry(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(co["id"], trial_ends_at=NOW - timedelta(days=1))
    await _seed_tokens(co["id"], 0)

    async with AsyncSessionLocal() as s:
        with pytest.raises(HTTPException) as exc:
            await subscription_service.enforce_token_quota(s, co["id"])
    assert exc.value.status_code == 402
    assert "已冻结" in exc.value.detail


async def test_unlimited_quota_never_blocks(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _set_subscription(
        co["id"], plan_code="enterprise", status=SubscriptionStatus.ACTIVE,
        trial_ends_at=None, period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=29),
        seats_limit=None, monthly_token_quota=None)
    await _seed_tokens(co["id"], 10_000_000)

    async with AsyncSessionLocal() as s:
        # 不限量 → 不抛
        await subscription_service.enforce_token_quota(s, co["id"])
        await subscription_service.enforce_seat_limit(
            s, co["id"], adding=1000, label="扩编")
