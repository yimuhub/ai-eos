"""账单 / 用量 / 席位对账（E3 收尾）。

这些用例**不复用** `usage_service` 的聚合作为期望值：种子流水直接写进
`token_usages`，期望值在测试里用 `func.sum` / `group_by` 从原始行现算，
再与 HTTP 响应逐项比对，避免"拿实现验证实现"。

金额只做 Decimal 容差比较（SQLite 的 float SUM 天生带尾差，且分桶各自
四舍五入后再回加会有 ≤0.005/桶 的舍入差）；token 数要求精确相等。

覆盖：
1. 期间总额（含零成本行、边界行、期间外行）与原始 SQL 对账；
2. by_model / by_day 分桶既能逐桶对上原始 SQL，又能回加总额；
3. CSV 汇总行与 /billing/usage 一致；
4. /license/status 与 /billing/usage 的席位 = 在编 Agent 原始计数
   （DISABLED 除外，PAUSED 仍占席）；
5. 跨公司防串账：B 的流水不得进入 A 的总额与分桶。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.token_usage import TokenUsage
from app.services import license_service

CENT = Decimal("0.01")

# CSV 列顺序来自 app/api/routes/licensing.py 的 CSV_COLUMNS（已先核对真实表头）
CSV_HEADER = [
    "company_id", "period_start", "period_end",
    "seats_limit", "seats_used", "agents_active",
    "tokens_input", "tokens_output", "tokens_total",
    "cost", "currency", "deliverables",
]

PERIOD_FROM = "2026-05-01"
PERIOD_TO = "2026-05-10"

# (model_name, created_at, input_tokens, output_tokens, total_cost)
# 期间内 6 行：含零成本、NULL 模型（归入 unknown）、首日 00:00 与末日 23:59:59 边界。
# 期间外 2 行：04-30 的最后一秒 / 05-11 的零点，必须被排除。
COMPANY_A_ROWS = [
    ("model-alpha", "2026-05-01T00:00:00", 10, 5, 0.0),
    ("model-alpha", "2026-05-02T09:30:00", 120, 80, 0.12),
    ("model-alpha", "2026-05-02T18:45:00", 30, 20, 0.0),
    ("model-beta", "2026-05-02T12:00:00", 200, 100, 0.31),
    (None, "2026-05-05T08:00:00", 40, 10, 0.05),
    ("model-beta", "2026-05-10T23:59:59", 60, 40, 0.07),
    # 期间外
    ("model-alpha", "2026-04-30T23:59:59", 900, 900, 9.0),
    ("model-beta", "2026-05-11T00:00:00", 800, 800, 8.0),
]

# A 期间内的期望值（直接在测试里由上面的种子行求和，独立于 SQL 与服务实现）
EXPECTED_A = {
    "input": 460,
    "output": 255,
    "total": 715,
    "cost": Decimal("0.55"),
    "models": {"model-alpha", "model-beta", "unknown"},
    "days": ["2026-05-01", "2026-05-02", "2026-05-05", "2026-05-10"],
}

COMPANY_B_ROWS = [
    ("model-gamma", "2026-05-02T12:00:00", 1000, 500, 5.0),
    # 故意复用 A 的模型名，验证 B 的流水不会混进 A 的同名桶
    ("model-alpha", "2026-05-03T12:00:00", 7, 3, 0.01),
]


@pytest.fixture(autouse=True)
def _community_mode(monkeypatch, tmp_path):
    """每个用例从"未配置 License"开始，币种回落 CNY。"""
    monkeypatch.delenv(license_service.LICENSE_ENV, raising=False)
    monkeypatch.delenv(license_service.PUBKEY_ENV, raising=False)
    monkeypatch.delenv("AIEOS_BILLING_CURRENCY", raising=False)
    monkeypatch.setenv(license_service.LICENSE_FILE_ENV,
                       str(tmp_path / "license.json"))
    license_service.reset_cache()
    yield
    license_service.reset_cache()


# ---------------------------------------------------------------------------
# 独立口径工具
# ---------------------------------------------------------------------------

def _dec(value: object) -> Decimal:
    """float/int/Decimal/None → Decimal（经 str，保留 SQLite 浮点尾差原样）。"""
    return Decimal(str(0 if value is None else value))


def _day_key(value: object) -> str:
    """SQLite 的 date() 返回字符串，PG 返回 date/datetime，统一成 YYYY-MM-DD。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _model_key(value: object) -> str:
    return "unknown" if value is None else str(value)


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """与服务同口径的 naive UTC [start, end+1d) 边界，但在这里独立声明。"""
    return (datetime.combine(start, time.min),
            datetime.combine(end + timedelta(days=1), time.min))


async def _seed_usage_rows(company_id: str, rows) -> None:
    async with AsyncSessionLocal() as s:
        for model, created_at, input_tokens, output_tokens, cost in rows:
            s.add(TokenUsage(
                company_id=company_id, model_name=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                input_cost=cost, output_cost=0.0, total_cost=cost,
                created_at=datetime.fromisoformat(created_at),
            ))
        await s.commit()


async def _raw_totals(company_id: str, start: date,
                      end: date) -> dict:
    lo, hi = _bounds(start, end)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
                func.count(),
            ).where(
                TokenUsage.company_id == company_id,
                TokenUsage.created_at >= lo,
                TokenUsage.created_at < hi,
            ))).one()
    return {
        "input": int(row[0] or 0),
        "output": int(row[1] or 0),
        "total": int(row[2] or 0),
        "cost": _dec(row[3]),
        "rows": int(row[4] or 0),
    }


async def _raw_lifetime_total(company_id: str) -> int:
    """不加时间过滤的全表 token 数，用来证明期间外行确实被排除。"""
    async with AsyncSessionLocal() as s:
        total = (await s.execute(
            select(func.coalesce(func.sum(TokenUsage.total_tokens), 0)).where(
                TokenUsage.company_id == company_id))).scalar()
    return int(total or 0)


async def _raw_grouped(company_id: str, start: date, end: date,
                       label, key_fn) -> dict:
    lo, hi = _bounds(start, end)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(
                label.label("bucket"),
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
            ).where(
                TokenUsage.company_id == company_id,
                TokenUsage.created_at >= lo,
                TokenUsage.created_at < hi,
            ).group_by(label))).all()
    out = {}
    for key, input_tokens, output_tokens, total, cost in rows:
        out[key_fn(key)] = {
            "input": int(input_tokens or 0),
            "output": int(output_tokens or 0),
            "total": int(total or 0),
            "cost": _dec(cost),
        }
    return out


async def _raw_active_agents(company_id: str) -> int:
    """独立席位口径：该公司 status != DISABLED 的 Agent 行数。"""
    async with AsyncSessionLocal() as s:
        return int((await s.execute(
            select(func.count()).select_from(Agent).where(
                Agent.company_id == company_id,
                Agent.status != AgentStatus.DISABLED,
            ))).scalar() or 0)


async def _set_agent_status(agent_id: str, status: AgentStatus) -> None:
    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_id)
        agent.status = status
        await s.commit()


async def _get_usage(client, company_id: str, user: dict) -> dict:
    r = await client.get("/billing/usage", params={
        "company_id": company_id,
        "from": PERIOD_FROM, "to": PERIOD_TO,
    }, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _assert_buckets_reconcile(buckets: list[dict], raw: dict,
                              key: str) -> None:
    """每个桶的 token 精确相等，金额在 1 分容差内；桶集合与原始分组一致。"""
    assert sorted(b[key] for b in buckets) == sorted(raw)
    for bucket in buckets:
        expected = raw[bucket[key]]
        assert bucket["tokens"] == {
            "input": expected["input"],
            "output": expected["output"],
            "total": expected["total"],
        }, f"{key}={bucket[key]} token 桶与原始 SQL 不一致"
        assert abs(_dec(bucket["cost"]) - expected["cost"]) <= CENT, (
            f"{key}={bucket[key]} 金额与原始 SQL 不一致："
            f"{bucket['cost']} vs {expected['cost']}")


def _assert_buckets_roll_up(buckets: list[dict], totals: dict,
                            total_cost: object) -> None:
    """分桶回加：token 精确相等；金额允许每桶 ≤1 分舍入累积。"""
    for field in ("input", "output", "total"):
        assert sum(b["tokens"][field] for b in buckets) == totals[field]
    bucket_cost = sum(_dec(b["cost"]) for b in buckets)
    assert abs(bucket_cost - _dec(total_cost)) <= CENT * len(buckets)


# ---------------------------------------------------------------------------
# 1) 用量总额 / 分桶对账
# ---------------------------------------------------------------------------

async def test_usage_totals_and_buckets_reconcile_with_raw_sql(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    await _seed_usage_rows(company["id"], COMPANY_A_ROWS)
    start, end = date.fromisoformat(PERIOD_FROM), date.fromisoformat(PERIOD_TO)

    body = await _get_usage(client, company["id"], owner)

    # --- 总额 vs 原始 SQL ---
    raw = await _raw_totals(company["id"], start, end)
    assert raw["rows"] == 6, "期间内应有 6 行；边界/期间外行数不对"
    assert body["tokens"] == {
        "input": raw["input"], "output": raw["output"], "total": raw["total"],
    }
    assert abs(_dec(body["cost"]) - raw["cost"]) <= CENT
    # 再与测试内直接求和（不经过 SQL）的期望值对一遍
    assert body["tokens"] == {
        "input": EXPECTED_A["input"],
        "output": EXPECTED_A["output"],
        "total": EXPECTED_A["total"],
    }
    assert abs(_dec(body["cost"]) - EXPECTED_A["cost"]) <= CENT
    assert body["currency"] == "CNY"

    # --- 期间外行确实被排除 ---
    assert await _raw_lifetime_total(company["id"]) > raw["total"]
    assert {d["date"] for d in body["by_day"]} == set(EXPECTED_A["days"])

    # --- by_model / by_day vs 原始分组 ---
    raw_by_model = await _raw_grouped(
        company["id"], start, end,
        func.coalesce(TokenUsage.model_name, "unknown"), _model_key)
    raw_by_day = await _raw_grouped(
        company["id"], start, end, func.date(TokenUsage.created_at), _day_key)
    assert {m["model"] for m in body["by_model"]} == EXPECTED_A["models"]
    _assert_buckets_reconcile(body["by_model"], raw_by_model, "model")
    _assert_buckets_reconcile(body["by_day"], raw_by_day, "date")

    # --- 分桶回加总额 ---
    _assert_buckets_roll_up(body["by_model"], body["tokens"], body["cost"])
    _assert_buckets_roll_up(body["by_day"], body["tokens"], body["cost"])

    # 分桶金额回加也应贴近 API 返回的总额字符串
    for buckets in (body["by_model"], body["by_day"]):
        rolled = sum(_dec(b["cost"]) for b in buckets)
        assert abs(rolled - _dec(body["cost"])) <= CENT * len(buckets)


# ---------------------------------------------------------------------------
# 2) CSV 与 /billing/usage 对账
# ---------------------------------------------------------------------------

async def test_invoice_csv_reconciles_with_usage_totals(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    await _seed_usage_rows(company["id"], COMPANY_A_ROWS)

    usage = await _get_usage(client, company["id"], owner)
    r = await client.get("/billing/invoices/export.csv", params={
        "company_id": company["id"],
        "from": PERIOD_FROM, "to": PERIOD_TO,
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text

    reader = csv.DictReader(io.StringIO(r.text))
    assert reader.fieldnames == CSV_HEADER
    rows = list(reader)
    assert len(rows) == 1, "汇总账单应只有一行数据"
    row = rows[0]

    assert row["company_id"] == company["id"]
    assert row["period_start"] == PERIOD_FROM
    assert row["period_end"] == PERIOD_TO
    assert row["seats_limit"] == ""          # 社区模式不限席位
    assert int(row["seats_used"]) == usage["seats"]["used"]
    assert int(row["agents_active"]) == usage["agents_active"]
    assert int(row["tokens_input"]) == usage["tokens"]["input"]
    assert int(row["tokens_output"]) == usage["tokens"]["output"]
    assert int(row["tokens_total"]) == usage["tokens"]["total"]
    assert _dec(row["cost"]) == _dec(usage["cost"])
    assert row["currency"] == usage["currency"]
    assert int(row["deliverables"]) == usage["deliverables"]

    # CSV 也直接对上原始 SQL 与测试内期望值
    assert int(row["tokens_total"]) == EXPECTED_A["total"]
    assert abs(_dec(row["cost"]) - EXPECTED_A["cost"]) <= CENT


# ---------------------------------------------------------------------------
# 3) 席位对账：/license/status 与 /billing/usage vs 在编 Agent 原始计数
# ---------------------------------------------------------------------------

async def test_seat_usage_reconciles_with_active_agents(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    other_owner = await make_user()
    other_company = await make_company(other_owner)

    agent_ids = []
    for name in ("员工一", "员工二", "员工三", "员工四"):
        created = await client.post("/agents", json={
            "company_id": company["id"], "name": name,
        }, headers=owner["headers"])
        assert created.status_code == 201, created.text
        agent_ids.append(created.json()["id"])

    # 另一家公司的 Agent 不得计入 A 的席位
    external = await client.post("/agents", json={
        "company_id": other_company["id"], "name": "B 员工",
    }, headers=other_owner["headers"])
    assert external.status_code == 201, external.text

    async def _status_seats() -> int:
        resp = await client.get("/license/status", params={
            "company_id": company["id"]}, headers=owner["headers"])
        assert resp.status_code == 200, resp.text
        return resp.json()["seats"]["used"]

    # 初始 4 名在编
    assert await _raw_active_agents(company["id"]) == 4
    assert await _status_seats() == 4
    usage = await _get_usage(client, company["id"], owner)
    assert usage["seats"]["used"] == 4
    assert usage["agents_active"] == 4

    # 停用 1 名 → 3（DISABLED 不占席）
    await _set_agent_status(agent_ids[0], AgentStatus.DISABLED)
    assert await _raw_active_agents(company["id"]) == 3
    assert await _status_seats() == 3
    usage = await _get_usage(client, company["id"], owner)
    assert usage["seats"]["used"] == 3
    assert usage["agents_active"] == 3

    # PAUSED 仍占席（口径边界，不能被"非 RUNNING 即离席"偷换）
    await _set_agent_status(agent_ids[1], AgentStatus.PAUSED)
    assert await _raw_active_agents(company["id"]) == 3
    assert await _status_seats() == 3
    usage = await _get_usage(client, company["id"], owner)
    assert usage["seats"]["used"] == 3
    assert usage["agents_active"] == 3

    # 再停用 1 名 → 2
    await _set_agent_status(agent_ids[2], AgentStatus.DISABLED)
    assert await _raw_active_agents(company["id"]) == 2
    assert await _status_seats() == 2
    usage = await _get_usage(client, company["id"], owner)
    assert usage["seats"]["used"] == 2
    assert usage["agents_active"] == 2


# ---------------------------------------------------------------------------
# 4) 跨公司防串账
# ---------------------------------------------------------------------------

async def test_cross_company_usage_does_not_leak_into_totals(
        client, tenants):
    alice, alice_co = tenants.alice, tenants.alice_co
    bob, bob_co = tenants.bob, tenants.bob_co
    start, end = date.fromisoformat(PERIOD_FROM), date.fromisoformat(PERIOD_TO)

    await _seed_usage_rows(alice_co["id"], COMPANY_A_ROWS)
    await _seed_usage_rows(bob_co["id"], COMPANY_B_ROWS)

    body_a = await _get_usage(client, alice_co["id"], alice)
    body_b = await _get_usage(client, bob_co["id"], bob)

    raw_a = await _raw_totals(alice_co["id"], start, end)
    raw_b = await _raw_totals(bob_co["id"], start, end)

    # A 的总额 = A 自己的原始 SQL 口径（与合并口径不同）
    assert body_a["tokens"] == {
        "input": raw_a["input"], "output": raw_a["output"],
        "total": raw_a["total"],
    }
    assert body_a["tokens"] == {
        "input": EXPECTED_A["input"],
        "output": EXPECTED_A["output"],
        "total": EXPECTED_A["total"],
    }
    assert abs(_dec(body_a["cost"]) - raw_a["cost"]) <= CENT
    assert abs(_dec(body_a["cost"]) - EXPECTED_A["cost"]) <= CENT
    assert body_a["tokens"]["total"] != raw_a["total"] + raw_b["total"]

    # B 独有的模型不得出现在 A；B 复用 A 的模型名也不得抬高 A 的同名桶
    models_a = {m["model"] for m in body_a["by_model"]}
    assert "model-gamma" not in models_a
    assert models_a == EXPECTED_A["models"]
    raw_alpha_a = await _raw_grouped(
        alice_co["id"], start, end,
        func.coalesce(TokenUsage.model_name, "unknown"), _model_key)
    _assert_buckets_reconcile(body_a["by_model"], raw_alpha_a, "model")
    alpha_bucket = next(
        m for m in body_a["by_model"] if m["model"] == "model-alpha")
    assert alpha_bucket["tokens"]["total"] == 265

    # B 自己的口径同样对得上（且与 A 不同）
    assert body_b["tokens"] == {
        "input": raw_b["input"], "output": raw_b["output"],
        "total": raw_b["total"],
    }
    assert body_b["tokens"]["total"] == 1510
    assert {m["model"] for m in body_b["by_model"]} == {
        "model-alpha", "model-gamma"}
    _assert_buckets_roll_up(body_b["by_model"], body_b["tokens"], body_b["cost"])
    assert body_a["tokens"] != body_b["tokens"]
    assert bob_co["id"] != alice_co["id"]
