"""订阅与试用（E2）。

## 产品决策

订阅制 + **14 天试用**。套餐只描述"能力边界"（席位 / 月度 token 配额），
**不在这里写价格**：金额属于商业决策，未确认前不编造，路由与前端也不展示。

## 时间口径

全仓库统一使用 **naive UTC**（`datetime.now(timezone.utc).replace(tzinfo=None)`），
与 `token_usages.created_at`、`token_budgets` 的日窗口径一致。带时区的入参
在比较前会先归一到 naive UTC，避免"8 小时错位"这类只有跨时区才暴露的 bug。

## 状态

数据库里只存四种状态（`SubscriptionStatus`）。"试用已过期"不是落库状态，
而是展示态：`effective_status()` 依据 `trial_ends_at` 计算 `TRIAL_EXPIRED`，
**不改写原 status**——这样续费/补录后仍能还原真实订阅状态。
`ACTIVE` 但 `period_end` 已过 → 展示 `PAST_DUE`。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import CompanySubscription, SubscriptionStatus

TRIAL_DAYS = 14
PERIOD_DAYS = 30

# 套餐目录（纯常量）。`monthly_token_quota=None` 表示不限量；
# `seats_limit=None` 表示不限席位。金额刻意缺席。
PLAN_CATALOG: dict[str, dict] = {
    "trial": {
        "plan_code": "trial",
        "label": "试用",
        "trial_days": TRIAL_DAYS,
        "seats_limit": 10,
        "monthly_token_quota": 1_000_000,
    },
    "pro": {
        "plan_code": "pro",
        "label": "专业版",
        "trial_days": None,
        "seats_limit": 50,
        "monthly_token_quota": 20_000_000,
    },
    "enterprise": {
        "plan_code": "enterprise",
        "label": "企业版",
        "trial_days": None,
        "seats_limit": None,
        "monthly_token_quota": None,
    },
}

VALID_PLAN_CODES = frozenset(PLAN_CATALOG)


def utcnow() -> datetime:
    """当前 naive UTC，与仓库其它模块保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _status_value(status) -> str:
    return status.value if isinstance(status, SubscriptionStatus) else str(status)


def effective_status(sub: CompanySubscription,
                     now: datetime | None = None) -> str:
    """展示用状态：过期试用 → TRIAL_EXPIRED；过期 ACTIVE → PAST_DUE。

    纯函数：不修改 `sub.status`，调用方拿到的仍是落库状态。
    """
    current = _naive_utc(now) or utcnow()
    status = _status_value(sub.status)
    trial_end = _naive_utc(sub.trial_ends_at)
    if status == SubscriptionStatus.TRIALING.value and trial_end \
            and trial_end <= current:
        return "TRIAL_EXPIRED"
    period_end = _naive_utc(sub.period_end)
    if status == SubscriptionStatus.ACTIVE.value and period_end \
            and period_end <= current:
        return SubscriptionStatus.PAST_DUE.value
    return status


async def _find(db: AsyncSession,
                company_id: str) -> CompanySubscription | None:
    return (await db.execute(
        select(CompanySubscription)
        .where(CompanySubscription.company_id == company_id)
        .limit(1))).scalars().first()


def _new_trial(company_id: str, now: datetime) -> CompanySubscription:
    plan = PLAN_CATALOG["trial"]
    return CompanySubscription(
        company_id=company_id,
        plan_code=plan["plan_code"],
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=now + timedelta(days=plan["trial_days"] or TRIAL_DAYS),
        seats_limit=plan["seats_limit"],
        monthly_token_quota=plan["monthly_token_quota"],
    )


async def get_or_create(db: AsyncSession, company_id: str,
                        now: datetime | None = None) -> CompanySubscription:
    """取公司订阅；没有则自动开一个 14 天试用（只 flush，不 commit）。"""
    sub = await _find(db, company_id)
    if sub is not None:
        return sub
    sub = _new_trial(company_id, _naive_utc(now) or utcnow())
    db.add(sub)
    await db.flush()
    return sub


async def start_trial(db: AsyncSession, company_id: str,
                      now: datetime | None = None) -> CompanySubscription:
    """主动领取试用。幂等语义是"只能一次"：已有订阅 → 409。"""
    if await _find(db, company_id) is not None:
        raise HTTPException(409, "该企业已领取过试用，不能重复领取")
    sub = _new_trial(company_id, _naive_utc(now) or utcnow())
    db.add(sub)
    await db.flush()
    return sub


async def change_plan(db: AsyncSession, company_id: str, plan_code: str,
                      now: datetime | None = None) -> CompanySubscription:
    """切换套餐：套餐必须有效；置 ACTIVE 并开一个 30 天计费周期。"""
    plan = PLAN_CATALOG.get(plan_code)
    if plan is None:
        raise HTTPException(400, f"未知套餐：{plan_code}")
    current = _naive_utc(now) or utcnow()
    sub = await get_or_create(db, company_id, current)
    sub.plan_code = plan["plan_code"]
    sub.status = SubscriptionStatus.ACTIVE
    sub.period_start = current
    sub.period_end = current + timedelta(days=PERIOD_DAYS)
    sub.seats_limit = plan["seats_limit"]
    sub.monthly_token_quota = plan["monthly_token_quota"]
    await db.flush()
    return sub


async def cancel(db: AsyncSession, company_id: str,
                 now: datetime | None = None) -> CompanySubscription:
    """取消订阅（幂等：已取消的公司再次取消不报错、不改数据）。"""
    sub = await get_or_create(db, company_id, now)
    if _status_value(sub.status) != SubscriptionStatus.CANCELED.value:
        sub.status = SubscriptionStatus.CANCELED
        await db.flush()
    return sub


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def to_public(sub: CompanySubscription,
              now: datetime | None = None) -> dict:
    """对外结构：只暴露套餐/状态/周期/配额，不含任何计费金额。"""
    return {
        "company_id": sub.company_id,
        "plan_code": sub.plan_code,
        "status": _status_value(sub.status),
        "effective_status": effective_status(sub, now),
        "trial_ends_at": _iso(sub.trial_ends_at),
        "period": {
            "start": _iso(sub.period_start),
            "end": _iso(sub.period_end),
        },
        "limits": {
            "seats": sub.seats_limit,
            "monthly_tokens": sub.monthly_token_quota,
        },
        # 扁平字段与嵌套结构并存：前端既可按组渲染，也可直接取值。
        "period_start": _iso(sub.period_start),
        "period_end": _iso(sub.period_end),
        "seats_limit": sub.seats_limit,
        "monthly_token_quota": sub.monthly_token_quota,
    }
