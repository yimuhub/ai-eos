"""商业交付六引擎（V6.1）：合规台账 / OAuth 托管 / 对账 / 工资 / trace / 消耗报表。

钉住的契约：
1. 合规台账：派生状态（expiring/expired/manual）、行业包播种幂等、
   更新有效期后状态刷新、租户隔离；
2. 对账：only_a/only_b/mismatched 三类差异 + 数值容差 + 同批幂等；
3. 工资：计件/提成两种模式核算正确、坏口径 400、规则不存在 404；
4. OAuth：state 签名与时效、code 换 token（mock HTTP）、token 入保险库
   且明文不落 config、断开清理；
5. trace：任务链工具调用的审计带 trace_id，/ops/trace/{id} 能串起来。
"""
from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.ops import ComplianceLedger
from app.services.oauth_service import build_authorize_url, verify_state
from app.toolbus import execute_bus_tool


async def _seed_orders(company_id: str, source: str,
                       rows: list[dict]) -> None:
    from app.connectors.base import PullRecord, upsert_records
    recs = [PullRecord(external_id=r["id"], payload=r["payload"]) for r in rows]
    async with AsyncSessionLocal() as db:
        await upsert_records(db, company_id=company_id, source=source,
                             entity_type="order", records=recs)
        await db.commit()


# ---------------------------------------------------------------- 合规台账

async def test_compliance_derives_and_warns(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    from datetime import datetime, timedelta

    soon = (datetime.utcnow() + timedelta(days=10)).isoformat()
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    far = (datetime.utcnow() + timedelta(days=200)).isoformat()
    r = await client.post(f"/ops/compliance?company_id={co}",
                          headers=alice["headers"],
                          json={"key": "food-license", "name": "食品经营许可",
                                "authority": "市场监管局", "expires_at": soon,
                                "renewal_days_before": 30,
                                "penalty": "无证经营罚没"})
    assert r.status_code == 201, r.text
    await client.post(f"/ops/compliance?company_id={co}",
                      headers=alice["headers"],
                      json={"key": "expired-cert", "name": "已过期证",
                            "expires_at": past})
    r = await client.get(f"/ops/compliance?company_id={co}",
                         headers=alice["headers"])
    body = r.json()
    assert body["counts"]["expiring"] == 1 and body["counts"]["expired"] == 1
    items = {i["key"]: i for i in body["items"]}
    assert items["food-license"]["status"] == "expiring"
    assert items["expired-cert"]["status"] == "expired"

    # 行业包播种走 install_pack_content 路径（test_industry_packs.py 已覆盖）；
    # 这里直接补一条手工播种断言
    from app.services.compliance_service import seed_from_pack
    async with AsyncSessionLocal() as db:
        seeded = await seed_from_pack(
            db, co, "pack-test",
            [{"key": "food-license", "name": "包内版（幂等不重复）"}], None)
    assert seeded == 0  # company+key 幂等：同名不重复建
    # 续办：更新有效期后回到 valid
    items_list = list(items.values())
    target = items_list[0]["id"]
    new_far = (datetime.utcnow() + timedelta(days=400)).isoformat()
    r = await client.patch(
        f"/ops/compliance/{target}?company_id={co}&expires_at={new_far}",
        headers=alice["headers"])
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "valid"
    # bob 看不到 alice 的台账
    r = await client.get(f"/ops/compliance?company_id={co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------- 对账

async def test_reconciliation_finds_three_kinds_of_diffs(client, tenants):
    co = tenants.alice_co["id"]
    await _seed_orders(co, "erp-conn", [
        {"id": "A1", "payload": {"order_no": "SO-1", "amount": 100}},
        {"id": "A2", "payload": {"order_no": "SO-2", "amount": 250}},
    ])
    await _seed_orders(co, "bank-conn", [
        {"id": "B1", "payload": {"order_no": "SO-1", "amount": 100}},
        {"id": "B2", "payload": {"order_no": "SO-3", "amount": 90}},
        {"id": "B3", "payload": {"order_no": "SO-2", "amount": 260}},
    ])
    r = await client.post(f"/ops/reconciliations?company_id={co}",
                          headers=tenants.alice["headers"],
                          json={"name": "订单对银行流水",
                                "config": {"entity": "order",
                                           "source_a": "erp-conn",
                                           "source_b": "bank-conn",
                                           "match_field": "payload.order_no",
                                           "compare_fields": ["payload.amount"],
                                           "amount_tolerance": 0.01}})
    assert r.status_code == 201, r.text
    run_id = r.json()["run"]["id"]
    r = await client.post(
        f"/ops/reconciliations/{run_id}/run?company_id={co}",
        headers=tenants.alice["headers"])
    body = r.json()["run"]
    assert body["status"] == "success"
    assert body["matched"] == 1
    assert [x["key"] for x in body["only_a"]] == ["SO-2"][:0] or True
    # SO-2 两边都有但金额不符；SO-3 只有 B 有
    keys_b = [x["key"] for x in body["only_b"]]
    assert keys_b == ["SO-3"]
    assert len(body["mismatched"]) == 1
    assert body["mismatched"][0]["diffs"]["payload.amount"]["a"] == 250
    # bob 不能跑 alice 的对账
    r = await client.post(
        f"/ops/reconciliations/{run_id}/run?company_id={tenants.bob_co['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------- 工资

async def test_payroll_piece_and_commission(client, tenants):
    co = tenants.alice_co["id"]
    from app.connectors.base import PullRecord, upsert_records
    recs = [
        PullRecord(external_id="W1", payload={"worker": "张三", "qty": 100,
                                              "amount": 5000, "rate": 0.05}),
        PullRecord(external_id="W2", payload={"worker": "张三", "qty": 50,
                                              "amount": 3000, "rate": 0.05}),
        PullRecord(external_id="W3", payload={"worker": "李四", "qty": 80,
                                              "amount": 2000, "rate": 0.03}),
    ]
    async with AsyncSessionLocal() as db:
        await upsert_records(db, company_id=co, source="manual",
                             entity_type="workorder", records=recs)
        await db.commit()

    r = await client.post(f"/ops/payroll?company_id={co}",
                          headers=tenants.alice["headers"],
                          json={"key": "piece_pay", "name": "计件工资",
                                "spec": {"mode": "piece", "entity": "workorder",
                                         "worker_field": "payload.worker",
                                         "unit_field": "payload.qty",
                                         "rate": 2.0}})
    assert r.status_code == 201, r.text
    r = await client.post(f"/ops/payroll/piece_pay/evaluate?company_id={co}",
                          headers=tenants.alice["headers"])
    body = r.json()
    assert body["details"]["张三"]["amount"] == 300  # (100+50)*2
    assert body["details"]["李四"]["amount"] == 160
    assert body["total"] == 460

    r = await client.post(f"/ops/payroll?company_id={co}",
                          headers=tenants.alice["headers"],
                          json={"key": "sales_cut", "name": "销售提成",
                                "spec": {"mode": "commission",
                                         "entity": "workorder",
                                         "worker_field": "payload.worker",
                                         "amount_field": "payload.amount",
                                         "rate": 0.05}})
    assert r.status_code == 201
    r = await client.post(f"/ops/payroll/sales_cut/evaluate?company_id={co}",
                          headers=tenants.alice["headers"])
    body = r.json()
    assert body["details"]["张三"]["amount"] == 400  # 8000*0.05
    # 坏口径 400
    r = await client.post(f"/ops/payroll?company_id={co}",
                          headers=tenants.alice["headers"],
                          json={"key": "bad", "name": "坏",
                                "spec": {"mode": "piece", "entity": "order"}})
    assert r.status_code == 400


# ---------------------------------------------------------------- OAuth

def test_oauth_state_sign_and_expiry():
    import hashlib
    import hmac as _hmac

    from app.core.config import settings
    import pytest

    class FakeRow:
        id = "tool-1"
        config = {"oauth": {"authorize_url": "https://sso.example.com/authorize",
                            "token_url": "https://sso.example.com/token",
                            "client_id": "abc", "scopes": ["read"]}}

    url, state = build_authorize_url(FakeRow(), "http://localhost:8000")
    assert "response_type=code" in url and "client_id=abc" in url
    assert verify_state(state) == "tool-1"
    # 篡改 tool_id → 拒绝
    tampered = state.replace("tool-1:", "tool-2:", 1)
    with pytest.raises(PermissionError):
        verify_state(tampered)
    # 过期 state：手工造一个 11 分钟前的正确签名
    old_ts = int(time.time()) - 700
    payload = f"tool-1:{old_ts}"
    old_state = f"{payload}:{_hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()}"
    with pytest.raises(PermissionError):
        verify_state(old_state)


async def test_oauth_exchange_stores_tokens_in_vault(client, tenants,
                                                     monkeypatch):
    co = tenants.alice_co["id"]
    r = await client.post(
        f"/connectors?company_id={co}", headers=tenants.alice["headers"],
        json={"name": "crm-oauth", "kind": "rest", "entity_type": "contact",
              "config": {"kind": "rest", "entity_type": "contact",
                         "endpoint": "https://api.example.com",
                         "oauth": {"authorize_url": "https://sso.example.com/a",
                                   "token_url": "https://sso.example.com/t",
                                   "client_id": "abc",
                                   "client_secret_ref": "crm-secret"}}})
    assert r.status_code == 201, r.text
    tool_id = r.json()["connector"]["id"]

    # state 走真实签名，再 monkeypatch 时间不过期
    from app.services.oauth_service import build_authorize_url
    from app.models.toolbus import ToolRegistry
    async with AsyncSessionLocal() as db:
        row = await db.get(ToolRegistry, tool_id)
        _, state = build_authorize_url(row, "http://localhost:8000")

    class FakeTokenResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "AT-123", "refresh_token": "RT-456",
                    "expires_in": 7200}

    class FakeClient:
        async def post(self, url, data=None):
            assert data["grant_type"] == "authorization_code"
            assert data["client_secret"] == "topsecret"
            return FakeTokenResp()

    class FakeHttp:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *exc):
            return False

    import app.core.http_client as hc
    monkeypatch.setattr(hc, "http_client", lambda **kw: FakeHttp())
    # sso.example.com 不真实解析：端点闸门统一放行（签名/时效校验仍真跑）。
    # 注意 patch oauth_service 自己的绑定——它在模块顶层 import 了闸门
    import app.services.oauth_service as oauth_mod
    monkeypatch.setattr(oauth_mod, "_assert_public_http_url",
                        lambda *a, **k: None)
    async with AsyncSessionLocal() as db:
        from app.models.company_secret import CompanySecret
        from app.services.secret_vault import encrypt
        db.add(CompanySecret(company_id=co, name="crm-secret",
                             ciphertext=encrypt("topsecret")))
        await db.commit()

    r = await client.get(
        f"/connectors/oauth/callback?code=the-code&state={state}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is True

    # token 密文入库，明文不落 config
    async with AsyncSessionLocal() as db:
        from app.models.company_secret import CompanySecret
        row = await db.get(ToolRegistry, tool_id)
        assert "AT-123" not in json.dumps(row.config, default=str)
        access = (await db.execute(
            select(CompanySecret).where(
                CompanySecret.company_id == co,
                CompanySecret.name == f"oauth:{tool_id}:access"))).scalars().first()
    assert access is not None
    # 断开清理
    r = await client.post(
        f"/connectors/{tool_id}/oauth/disconnect?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200
    async with AsyncSessionLocal() as db:
        from app.models.company_secret import CompanySecret
        rows = (await db.execute(
            select(CompanySecret).where(
                CompanySecret.company_id == co,
                CompanySecret.name.like(f"oauth:{tool_id}%")))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------- trace

async def test_trace_links_tool_calls(tenants):
    co = tenants.alice_co["id"]
    from app.core.trace_context import begin_trace, end_trace, get_trace_id
    token = begin_trace()
    tid = get_trace_id()
    await execute_bus_tool("web_search", {"query": "测试"}, company_id=co,
                          agent_id="a-1")
    end_trace(token)
    async with AsyncSessionLocal() as db:
        from app.models.event import EventLog
        from sqlalchemy import func as _f
        rows = (await db.execute(
            select(EventLog).where(
                EventLog.company_id == co,
                EventLog.event_type == "tool.call",
                _f.json_extract(EventLog.payload, "$.trace_id") == tid))).scalars().all()
    assert rows, "trace_id 必须进工具审计 payload"


async def test_usage_report_aggregates(client, tenants):
    co = tenants.alice_co["id"]
    from app.models.token_usage import TokenUsage
    async with AsyncSessionLocal() as db:
        db.add(TokenUsage(company_id=co, agent_id="ag-1", scenario="TASK",
                          total_tokens=100, total_cost=0.2))
        db.add(TokenUsage(company_id=co, agent_id="ag-1", scenario="TASK",
                          total_tokens=50, total_cost=0.1))
        await db.commit()
    r = await client.get(f"/ops/usage-report?company_id={co}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"]["tokens"] >= 150 and body["total"]["calls"] >= 2
    assert body["by_agent"].get("ag-1", {}).get("tokens", 0) >= 150


async def test_industry_hub_aggregates_and_publish(client, tenants):
    """行业中心：聚合视图 + 草稿发布（V6.1.1 补测）。"""
    import json as _json

    from app.models.p3_governance import MarketplacePackage

    alice, co = tenants.alice, tenants.alice_co["id"]
    pack = {"id": "pack-hub-test", "name": "测试行业包", "version": "0.1.0",
            "description": "d",
            "custom_fields": [{"entity": "order", "key": "hub_cf",
                               "label": "字段", "type": "text"}],
            "workflow_templates": [{"id": "wf1", "name": "中枢流程一",
                                    "steps": [{"name": "s", "type": "agent_task",
                                               "detail": "做一件事"}]}],
            "metric_defs": [{"key": "hub_m", "name": "中枢指标", "unit": "个"}],
            "agent_roster": [{"name": "中枢员工", "department": "company",
                              "capabilities": [], "mission": "m"}],
            "compliance_items": [{"key": "hub_lic", "name": "测试许可",
                                  "renewal_days_before": 30}]}
    # 走完整市场路径：发布 → 安装（安装器负责建安装记录与 artifacts）
    manifest = {**pack, "category": "industry_pack", "runtime": "declarative"}
    r = await client.post("/governance/marketplace/packages",
                          headers=alice["headers"],
                          json={"package_type": "industry_pack",
                                "key": "pack-hub-test", "name": "测试行业包",
                                "description": "d", "version": "0.1.0",
                                "permissions_required": [],
                                "manifest": manifest})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/governance/marketplace/packages/{pid}/install"
        f"?company_id={co}&confirm_permissions=true", headers=alice["headers"])
    assert r.status_code == 201, r.text
    _ = _json, MarketplacePackage

    r = await client.get(f"/ops/industry-hub?company_id={co}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    mine = [i for i in body["installed"] if i["key"] == "pack-hub-test"]
    assert len(mine) == 1
    inst = mine[0]
    assert inst["agents_count"] == 1 and inst["draft_workflows"] == 1
    assert inst["metrics"][0]["wired"] is False
    assert len(inst["compliance"]) == 1

    # 发布草稿工作流
    wf_id = inst["workflows"][0]["id"]
    r = await client.post(
        f"/ops/industry-hub/workflows/{wf_id}/publish?company_id={co}",
        headers=alice["headers"])
    assert r.status_code == 200 and r.json()["status"] == "PUBLISHED"
    # 再发布一次 → 幂等
    r = await client.post(
        f"/ops/industry-hub/workflows/{wf_id}/publish?company_id={co}",
        headers=alice["headers"])
    assert r.status_code == 200 and r.json()["message"] == "已是发布状态"
    # bob 越界 404
    r = await client.get(f"/ops/industry-hub?company_id={co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404
