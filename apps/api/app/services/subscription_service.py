"""订阅、试用与套餐配额（E2）。

## 产品决策

订阅制 + **14 天试用**。套餐只描述"能力边界"（席位 / 月度 token 配额），
**不在这里写价格**：金额属于商业决策，未确认前不编造，路由与前端也不展示。

## 付费路径（为什么有「订单」）

此前 `POST /subscription/plan` 只校验公司 MANAGE，任何公司老板一次请求就能
把自己升到企业版 ACTIVE——付钱这个环节根本不存在。现在：

* **降级** = 自服务：公司所有者随时可以切到更低的套餐；
* **升级 / 续费** = 必须先有订单：申请人提交订单 → 平台确认收款（`is_superadmin`
  落章，线下转账/渠道后台在系统之外完成）→ 订单 PAID → 才能把套餐切成 ACTIVE。
  同一套餐再次切换等于续费，同样需要新订单，否则可以靠反复"切套餐"白拿周期。

订单里没有金额字段，理由同上：本仓库没有支付渠道，也不该编造价格。

## 配额的边界

`seats_limit` / `monthly_token_quota` 不再是只写不读的装饰：

* 席位：`enforce_seat_limit()` 在新增员工（含组织预设导入）前拦截；
* token：`enforce_token_quota()` 在每次 LLM 调用前按自然月累计拦截。

试用到期 / 取消且周期结束后按**冻结**处理（上限归零）：已有员工可以继续看
数据，但不能扩编、不能再烧 token。已超编的公司不会被回删员工，只是无法再新增。

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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import (CompanySubscription, SubscriptionOrder,
                                     SubscriptionOrderStatus,
                                     SubscriptionStatus)
from app.models.token_usage import TokenUsage

TRIAL_DAYS = 14
PERIOD_DAYS = 30

# 套餐目录（纯常量）。`monthly_token_quota=None` 表示不限量；
# `seats_limit=None` 表示不限席位。金额刻意缺席。
#
# 试用席位的下限不是拍脑袋：随包最大的行业预设（bigco_amazon）有 56 个岗位，
# 试用期若放不下"一键建公司"，这个核心卖点就没法体验。60 留了一点余量。
PLAN_CATALOG: dict[str, dict] = {
    "trial": {
        "plan_code": "trial",
        "label": "试用",
        "trial_days": TRIAL_DAYS,
        "seats_limit": 60,
        "monthly_token_quota": 1_000_000,
    },
    "pro": {
        "plan_code": "pro",
        "label": "专业版",
        "trial_days": None,
        "seats_limit": 200,
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

# 升级方向只看这个顺序：trial < pro < enterprise。降级自服务，升级要订单。
PLAN_RANK: dict[str, int] = {"trial": 0, "pro": 1, "enterprise": 2}

# 自服务不可切换到的套餐：试用是初始态，由 start_trial / get_or_create 产生。
SELF_SERVICE_PLANS = frozenset({"pro", "enterprise"})


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


def _order_status_value(status) -> str:
    return (status.value if isinstance(status, SubscriptionOrderStatus)
            else str(status))


def plan_label(plan_code: str) -> str:
    return (PLAN_CATALOG.get(plan_code) or {}).get("label", plan_code)


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


def effective_limits(sub: CompanySubscription,
                     now: datetime | None = None) -> tuple[int | None, int | None]:
    """(席位上限, 月度 token 上限)；`None` = 不限。

    试用到期、取消且周期结束后按冻结处理：上限归零。已超编的员工不受影响
    （不回删），只是不能再新增、不能再调用模型。
    """
    current = _naive_utc(now) or utcnow()
    status = effective_status(sub, current)
    if status == "TRIAL_EXPIRED":
        return 0, 0
    if status == SubscriptionStatus.CANCELED.value:
        period_end = _naive_utc(sub.period_end)
        if period_end is None or period_end <= current:
            return 0, 0
        return sub.seats_limit, sub.monthly_token_quota
    return sub.seats_limit, sub.monthly_token_quota


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


# ---------------------------------------------------------------------------
# 配额闸门
# ---------------------------------------------------------------------------

def month_start(now: datetime | None = None) -> datetime:
    """当前自然月 1 日 00:00（naive UTC）。"""
    current = _naive_utc(now) or utcnow()
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def tokens_used_this_month(db: AsyncSession, company_id: str,
                                 now: datetime | None = None) -> int:
    """本月已用 token（`token_usages.total_tokens` 求和）。"""
    total = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.created_at >= month_start(now)))).scalar()
    return int(total or 0)


async def enforce_seat_limit(db: AsyncSession, company_id: str, *,
                             adding: int = 1,
                             label: str = "新增员工") -> None:
    """新增席位前拦截：超出套餐上限 → 409。

    与 License 席位闸门（`routes/agents._enforce_license_seats`）是两道独立
    闸门：License 管"这份授权允许几个"，套餐管"你买的这一档允许几个"。
    套餐配额不做审批豁免——审批能绕过的话，商业上限就又变成了装饰。
    """
    from app.services import usage_service

    sub = await get_or_create(db, company_id)
    limit, _ = effective_limits(sub)
    if limit is None:
        return
    used = await usage_service.count_active_agents(db, company_id)
    if used + adding <= limit:
        return

    status = effective_status(sub)
    if limit == 0:
        why = ("试用已到期" if status == "TRIAL_EXPIRED"
               else "订阅已取消且计费周期已结束")
        raise HTTPException(
            409,
            f"{why}，当前套餐「{plan_label(sub.plan_code)}」已冻结："
            f"{label}需要先升级/续费套餐。"
            "请在「套餐与账单」提交升级订单，平台确认收款后立即恢复。")
    raise HTTPException(
        409,
        f"当前套餐「{plan_label(sub.plan_code)}」席位已满："
        f"上限 {limit}、已用 {used}，本次{label} {adding} 个会超出。"
        "请先升级套餐（提交升级订单 → 平台确认收款 → 立即生效）。")


# 配额闸门的 60s 进程内缓存。每个 LLM 调用（含每轮工具调用、每条群聊
# 发言）都会走到这里——不加缓存时每次都对 token_usages 做"本月求和"的
# 聚合扫描，活跃公司上万行流水被反复全读，是可感知的全局拖慢。月度配额
# 对 60 秒内的超量完全不敏感（一次调用至多几万 token，月额度以百万计），
# TTL 过期后自然回到精确口径。进程内字典即可：后台 worker 与请求处理
# 同进程，多 uvicorn worker 各自持有一份也只会多查几次库，不影响正确性。
_QUOTA_CACHE: dict[str, tuple[float, bool]] = {}
_QUOTA_TTL_S = 60.0


async def enforce_token_quota(db: AsyncSession, company_id: str,
                              now: datetime | None = None) -> None:
    """月度 token 配额闸门：到顶 → 402（Payment Required）。

    在每次 LLM 调用前执行；`quota=None` 视为不限量。统计口径与
    `/billing/usage` 一致（`token_usages` 流水求和），不做估算；
    结果按公司缓存 60 秒（见 _QUOTA_CACHE 说明）。
    """
    import time as _time

    current = _naive_utc(now) or utcnow()
    cache_key = f"{company_id}:{current.year}-{current.month}"
    cached = _QUOTA_CACHE.get(cache_key)
    if cached is not None and (_time.monotonic() - cached[0]) < _QUOTA_TTL_S:
        if cached[1]:
            raise HTTPException(
                402, "本月 token 配额已用尽（缓存判定）。模型调用已停止，"
                "下月 1 日自动恢复；如需立即恢复请升级套餐，或等待 1 分钟后重试。")
        return
    sub = await get_or_create(db, company_id, now)
    _, quota = effective_limits(sub, now)
    if quota is None:
        return
    used = await tokens_used_this_month(db, company_id, now)
    if used < quota:
        _QUOTA_CACHE[cache_key] = (_time.monotonic(), False)
        return
    _QUOTA_CACHE[cache_key] = (_time.monotonic(), True)
    if quota == 0:
        raise HTTPException(
            402,
            f"当前套餐「{plan_label(sub.plan_code)}」已冻结"
            f"（{effective_status(sub, now)}）：本月 token 配额为 0，"
            "模型调用已停止。请在「套餐与账单」升级/续费后继续。")
    raise HTTPException(
        402,
        f"本月 token 配额已用尽：{used:,} / {quota:,}。"
        "模型调用已停止，下月 1 日自动恢复；如需立即恢复请升级套餐。")


# ---------------------------------------------------------------------------
# 套餐订单（升级 / 续费）
# ---------------------------------------------------------------------------

async def _find_order(db: AsyncSession, order_id: str) -> SubscriptionOrder | None:
    return await db.get(SubscriptionOrder, order_id)


async def create_order(db: AsyncSession, company_id: str, plan_code: str,
                       requested_by: str | None, *,
                       note: str | None = None,
                       now: datetime | None = None) -> SubscriptionOrder:
    """提交升级/续费订单（PENDING）。降级不需要订单，直接走 `/plan`。"""
    if plan_code not in SELF_SERVICE_PLANS:
        raise HTTPException(400, f"未知套餐：{plan_code}")
    sub = await get_or_create(db, company_id, now)
    current_rank = PLAN_RANK.get(sub.plan_code, 0)
    if PLAN_RANK[plan_code] < current_rank:
        raise HTTPException(
            400,
            f"降级到「{plan_label(plan_code)}」不需要订单，"
            "请直接切换套餐（自服务、立即生效）。")

    pending = (await db.execute(
        select(SubscriptionOrder).where(
            SubscriptionOrder.company_id == company_id,
            SubscriptionOrder.plan_code == plan_code,
            SubscriptionOrder.status == SubscriptionOrderStatus.PENDING)
        .limit(1))).scalars().first()
    if pending is not None:
        raise HTTPException(
            409, f"已有一张待确认的「{plan_label(plan_code)}」订单"
                 f"（{pending.id}），请等待平台确认收款。")

    order = SubscriptionOrder(
        company_id=company_id, plan_code=plan_code,
        status=SubscriptionOrderStatus.PENDING,
        requested_by=requested_by, note=note)
    db.add(order)
    await db.flush()
    return order


async def confirm_order(db: AsyncSession, order_id: str, confirmed_by: str,
                        now: datetime | None = None) -> SubscriptionOrder:
    """平台确认收款（PENDING → PAID）。已 PAID/APPLIED 幂等返回。"""
    order = await _find_order(db, order_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    status = _order_status_value(order.status)
    if status in (SubscriptionOrderStatus.PAID.value,
                  SubscriptionOrderStatus.APPLIED.value):
        return order
    if status == SubscriptionOrderStatus.CANCELED.value:
        raise HTTPException(409, "订单已取消，不能确认收款")
    order.status = SubscriptionOrderStatus.PAID
    order.confirmed_by = confirmed_by
    order.confirmed_at = _naive_utc(now) or utcnow()
    await db.flush()
    return order


async def cancel_order(db: AsyncSession, order_id: str,
                       now: datetime | None = None) -> SubscriptionOrder:
    """取消订单：只允许 PENDING；已收款/已生效的订单不能撤销。"""
    order = await _find_order(db, order_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    status = _order_status_value(order.status)
    if status != SubscriptionOrderStatus.PENDING.value:
        raise HTTPException(409, f"订单状态为 {status}，不能取消")
    order.status = SubscriptionOrderStatus.CANCELED
    await db.flush()
    return order


def _apply_plan(sub: CompanySubscription, plan: dict, current: datetime, *,
                renew: bool) -> None:
    """把套餐写进订阅。`renew=True` 时开一个新的 30 天计费周期。"""
    sub.plan_code = plan["plan_code"]
    sub.status = SubscriptionStatus.ACTIVE
    sub.seats_limit = plan["seats_limit"]
    sub.monthly_token_quota = plan["monthly_token_quota"]
    if renew:
        sub.period_start = current
        sub.period_end = current + timedelta(days=PERIOD_DAYS)


async def apply_order(db: AsyncSession, company_id: str, order_id: str,
                      now: datetime | None = None) -> CompanySubscription:
    """把一张**已收款**的订单落到订阅上（PAID → APPLIED）。"""
    order = await _find_order(db, order_id)
    if order is None or order.company_id != company_id:
        raise HTTPException(404, "订单不存在")
    status = _order_status_value(order.status)
    if status == SubscriptionOrderStatus.APPLIED.value:
        return await get_or_create(db, company_id, now)
    if status != SubscriptionOrderStatus.PAID.value:
        raise HTTPException(
            409, f"订单状态为 {status}，需要平台确认收款后才能生效")
    current = _naive_utc(now) or utcnow()
    sub = await get_or_create(db, company_id, current)
    _apply_plan(sub, PLAN_CATALOG[order.plan_code], current, renew=True)
    order.status = SubscriptionOrderStatus.APPLIED
    order.applied_at = current
    await db.flush()
    return sub


async def change_plan(db: AsyncSession, company_id: str, plan_code: str,
                      now: datetime | None = None) -> CompanySubscription:
    """自服务套餐变更：只允许**降级**。

    升级和续费必须走订单（`create_order` → `confirm_order` → `apply_order`）。
    此前这里无条件置 ACTIVE 并重置 30 天周期，任何公司老板一次 POST 就能
    免费升到企业版、或者反复切换白拿周期——商业化轨道上最大的洞。
    """
    plan = PLAN_CATALOG.get(plan_code)
    if plan is None:
        raise HTTPException(400, f"未知套餐：{plan_code}")
    if plan_code not in SELF_SERVICE_PLANS:
        raise HTTPException(400, "试用套餐不能手动切回，请升级到付费套餐")
    current = _naive_utc(now) or utcnow()
    sub = await get_or_create(db, company_id, current)
    if PLAN_RANK[plan_code] >= PLAN_RANK.get(sub.plan_code, 0):
        raise HTTPException(
            402,
            f"升级/续费到「{plan_label(plan_code)}」需要先支付："
            "请提交升级订单，平台确认收款后自动生效。")
    # 降级立即生效，但不送新周期；已有周期保持不动。
    _apply_plan(sub, plan, current, renew=False)
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
        "plan_label": plan_label(sub.plan_code),
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


def order_to_public(order: SubscriptionOrder) -> dict:
    return {
        "id": order.id,
        "company_id": order.company_id,
        "plan_code": order.plan_code,
        "plan_label": plan_label(order.plan_code),
        "status": _order_status_value(order.status),
        "requested_by": order.requested_by,
        "confirmed_by": order.confirmed_by,
        "confirmed_at": _iso(order.confirmed_at),
        "applied_at": _iso(order.applied_at),
        "note": order.note,
        "created_at": _iso(order.created_at),
    }
