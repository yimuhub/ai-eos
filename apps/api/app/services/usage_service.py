"""席位 / 用量 / 交付物聚合（E3）。

## 口径（写死在这里，避免各页面各算一套）

* **席位（seat）= 该公司当前"在职"的 Agent 数**：`agents.status != DISABLED`。
  `AgentStatus` 没有 DELETED；停用（DISABLED）是移除/下线的等价状态，
  不占席位；IDLE / RUNNING / PAUSED / ERROR 视为仍占用席位
  （它们仍是公司编制内的员工）。
* **用量**：`token_usages` 中 `company_id` 匹配、`created_at` 落在
  `[from 00:00:00Z, to+1 00:00:00Z)` 的流水。token 取
  `input_tokens / output_tokens / total_tokens`，成本取 `total_cost`
  （流水里的快照单价，事后改价不影响历史）。
* **交付物**：`assets` 表中该公司在期间内**创建**的行数。选 `assets`
  而不是 `artifacts`：交付中心（/delivery）的事实源是 `assets`，
  `artifacts` 是评审稿，草稿阶段不该计入"已交付"。
* **日期**：全部按 UTC；入参 `YYYY-MM-DD`，`to` 当天全天有效（含）。
* **金额**：一律转成十进制定点字符串（两位小数），不把 float 直接交给
  前端/CSV，避免 `0.1 + 0.2 = 0.30000000000000004` 这类账目噪音。
* **币种**：`AIEOS_BILLING_CURRENCY`（默认 CNY，与 PricingConfig 默认一致）。
  这里不做汇率换算——不同币种的金额相加没有意义，发行方应保证同一部署
  只使用一种计费币种。
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentStatus
from app.models.asset import Asset
from app.models.token_usage import TokenUsage
from app.services import license_service

BILLING_CURRENCY_ENV = "AIEOS_BILLING_CURRENCY"
DEFAULT_CURRENCY = "CNY"

# 不占席位的状态：停用即下线。其余（含 PAUSED/ERROR）仍算占编。
SEAT_EXCLUDED_STATUSES = (AgentStatus.DISABLED,)


def billing_currency() -> str:
    value = (os.environ.get(BILLING_CURRENCY_ENV) or "").strip()
    return value.upper() if value else DEFAULT_CURRENCY


def money_str(value: object) -> str:
    """金额 → 两位小数定点字符串（四舍五入，十进制计算）。"""
    if value is None:
        return "0.00"
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return "0.00"
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_period(from_raw: str | None, to_raw: str | None
                 ) -> tuple[date, date]:
    """解析 `YYYY-MM-DD` 起止；缺省为「本 UTC 月 1 日 → 今天」。

    非法格式或 `from > to` 抛 ValueError（路由层转 400）。
    """
    today = datetime.now(timezone.utc).date()

    def _parse(raw: str | None, default: date, label: str) -> date:
        if raw is None or not raw.strip():
            return default
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{label} 必须是 YYYY-MM-DD") from exc

    start = _parse(from_raw, today.replace(day=1), "from")
    end = _parse(to_raw, today, "to")
    if start > end:
        raise ValueError("from 不能晚于 to")
    return start, end


def _utc_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, end+1 00:00) 的 **naive UTC** 边界。

    TokenUsage 的时间列是 naive DateTime（见 token_usage._now()），
    用带时区的 datetime 绑定在 PG（timestamp without time zone）上会失败，
    因此这里统一去掉 tzinfo。
    """
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end + timedelta(days=1), time.min)
    return start_dt, end_dt


def _utc_bounds_aware(start: date, end: date) -> tuple[datetime, datetime]:
    """Asset.created_at 是 timezone=True：PG 上必须用 aware UTC 才不依赖会话时区。"""
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), time.min,
                              tzinfo=timezone.utc)
    return start_dt, end_dt


async def count_active_agents(db: AsyncSession, company_id: str) -> int:
    """在编 Agent 数（席位已用）。"""
    total = (await db.execute(
        select(func.count()).select_from(Agent).where(
            Agent.company_id == company_id,
            Agent.status.notin_(SEAT_EXCLUDED_STATUSES),
        ))).scalar()
    return int(total or 0)


async def company_usage(db: AsyncSession, company_id: str,
                        start: date, end: date,
                        *, seat_limit: int | None = None) -> dict:
    """聚合一家公司在期间内的席位 / token / 成本 / 交付物。

    `seat_limit` 显式传入时不回查 License（便于账单单据按当时快照出数）；
    为 None 时取当前 License（社区模式 → None → 不限）。
    """
    start_dt, end_dt = _utc_bounds(start, end)
    asset_start, asset_end = _utc_bounds_aware(start, end)
    in_period = (
        TokenUsage.company_id == company_id,
        TokenUsage.created_at >= start_dt,
        TokenUsage.created_at < end_dt,
    )

    sum_input = func.coalesce(func.sum(TokenUsage.input_tokens), 0)
    sum_output = func.coalesce(func.sum(TokenUsage.output_tokens), 0)
    sum_total = func.coalesce(func.sum(TokenUsage.total_tokens), 0)
    sum_cost = func.coalesce(func.sum(TokenUsage.total_cost), 0.0)

    totals = (await db.execute(
        select(sum_input, sum_output, sum_total, sum_cost).where(*in_period)
    )).one()
    tokens_input, tokens_output, tokens_total, cost = (
        int(totals[0] or 0), int(totals[1] or 0), int(totals[2] or 0),
        totals[3])

    model_label = func.coalesce(TokenUsage.model_name, "unknown")
    model_rows = (await db.execute(
        select(model_label.label("model"), sum_input, sum_output, sum_total,
               sum_cost)
        .where(*in_period)
        .group_by(model_label)
        .order_by(model_label)
    )).all()

    day_label = func.date(TokenUsage.created_at)
    day_rows = (await db.execute(
        select(day_label.label("day"), sum_input, sum_output, sum_total,
               sum_cost)
        .where(*in_period)
        .group_by(day_label)
        .order_by(day_label)
    )).all()

    deliverables = (await db.execute(
        select(func.count()).select_from(Asset).where(
            Asset.company_id == company_id,
            Asset.created_at >= asset_start,
            Asset.created_at < asset_end,
        ))).scalar()

    active_agents = await count_active_agents(db, company_id)
    limit = seat_limit if seat_limit is not None else license_service.seat_limit()

    def _bucket(rows, key_name: str, key_fn) -> list[dict]:
        out = []
        for row in rows:
            out.append({
                key_name: key_fn(row[0]),
                "tokens": {
                    "input": int(row[1] or 0),
                    "output": int(row[2] or 0),
                    "total": int(row[3] or 0),
                },
                "cost": money_str(row[4]),
            })
        return out

    def _day_key(value) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]

    return {
        "company_id": company_id,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "seats": {"limit": limit, "used": active_agents},
        "agents_active": active_agents,
        "tokens": {
            "input": tokens_input,
            "output": tokens_output,
            "total": tokens_total,
        },
        "cost": money_str(cost),
        "currency": billing_currency(),
        "deliverables": int(deliverables or 0),
        "by_model": _bucket(model_rows, "model", lambda v: str(v or "unknown")),
        "by_day": _bucket(day_rows, "date", _day_key),
    }
