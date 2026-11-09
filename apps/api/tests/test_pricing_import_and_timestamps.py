"""P2 回归：参考报价一键导入 + 时间字段必须带时区。

- `POST /tokens/pricing/import-defaults`：公司模型一键铺参考价，已有配置
  跳过，未匹配参考价的建 0 价待填；跨租户调用必须 404。
- budget / member / company 列表的 created_at/updated_at 必须是带时区的
  ISO 字符串（Z 结尾）——前端 `new Date(x)` 对裸 naive 字符串按本地时间
  解析，会给用户整体偏移一个时区。
"""
from datetime import datetime

import pytest

pytestmark = pytest.mark.asyncio


async def _make_model(client, user, company_id: str, name: str,
                      model_id: str) -> str:
    r = await client.post("/models", json={
        "name": name, "provider_type": "openai_compat",
        "base_url": "http://127.0.0.1:9/v1", "api_key_env": "NOT_A_REAL_KEY_ENV",
        "model_id": model_id, "company_id": company_id},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assert_tz_iso(value, field: str) -> None:
    assert isinstance(value, str) and value, f"{field} 不是非空字符串：{value!r}"
    assert value.endswith("Z") or "+00:00" in value, \
        f"{field} 缺时区后缀（前端会按本地时间误解析）：{value!r}"
    datetime.fromisoformat(value.replace("Z", "+00:00"))


async def test_import_default_pricing(client, tenants):
    """一键导入：匹配到的按参考价、未匹配建 0 价、二次调用全部跳过。"""
    alice = tenants.alice
    co = tenants.alice_co["id"]
    await _make_model(client, alice, co, "Mini 代理", "vendor-gpt-4o-mini")
    await _make_model(client, alice, co, "私有模型", "house-model-x")

    r = await client.post("/tokens/pricing/import-defaults",
                          json={"company_id": co}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2 and body["unmatched"] == 1, body

    r = await client.get("/tokens/pricing", headers=alice["headers"])
    rows = {p["model_name"]: p for p in r.json()}
    assert rows["Mini 代理"]["input_price_per_m"] == 1.1, rows["Mini 代理"]
    assert rows["Mini 代理"]["output_price_per_m"] == 4.4, rows["Mini 代理"]
    assert rows["私有模型"]["input_price_per_m"] == 0
    assert "实际账单" in (rows["私有模型"]["note"] or "")

    r = await client.post("/tokens/pricing/import-defaults",
                          json={"company_id": co}, headers=alice["headers"])
    assert r.json() == {"created": 0, "skipped": 2, "unmatched": 0}


async def test_import_default_pricing_requires_manage(client, tenants):
    """B 不能给 A 公司铺计费标准（成本口径属于公司级配置）。"""
    r = await client.post("/tokens/pricing/import-defaults",
                          json={"company_id": tenants.alice_co["id"]},
                          headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_budget_member_company_timestamps_have_tz(client, tenants):
    """预算 / 成员 / 公司列表的时间字段必须带时区。"""
    alice = tenants.alice
    co = tenants.alice_co["id"]

    r = await client.post(f"/token-budgets?company_id={co}", json={
        "scope_type": "company", "limit_tokens": 1000,
        "limit_cost": 0, "period": "day"}, headers=alice["headers"])
    assert r.status_code in (200, 201), r.text
    budgets = (await client.get(f"/token-budgets?company_id={co}",
                                headers=alice["headers"])).json()["budgets"]
    assert budgets, "预算创建后列表为空"
    _assert_tz_iso(budgets[0]["created_at"], "budget.created_at")
    _assert_tz_iso(budgets[0]["updated_at"], "budget.updated_at")

    members = (await client.get(f"/companies/{co}/members",
                                headers=alice["headers"])).json()
    assert members, "成员列表为空"
    _assert_tz_iso(members[0]["created_at"], "member.created_at")
    _assert_tz_iso(members[0]["updated_at"], "member.updated_at")

    companies = (await client.get("/company-manage/list",
                                  headers=alice["headers"])).json()["companies"]
    assert companies, "公司列表为空"
    _assert_tz_iso(companies[0]["created_at"], "company.created_at")
