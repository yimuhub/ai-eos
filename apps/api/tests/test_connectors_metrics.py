"""连接器 SDK / 规整层 / 指标层（V6 M3）。

钉住的契约：
1. REST 连接器同步：映射 + 幂等 upsert + 水位推进 + 同步账；
2. webhook ingest：签名校验、幂等；
3. 指标：结构化口径求值（count/sum/分组）+ 租户隔离；
4. metrics_query 总线工具：员工与外部 Agent（aieos.query_metric）取数同源；
5. 私网 endpoint 拒收。
"""
from __future__ import annotations

import json
from unittest.mock import patch

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.canonical import CanonicalRecord, ConnectorSyncRun
from app.toolbus import execute_bus_tool

REST_CONFIG = {
    "kind": "rest", "entity_type": "order",
    "endpoint": "https://erp.example.com",
    "list_path": "/api/orders",
    "id_field": "bill_no", "updated_field": "modified_time",
    "mapping": {"order_no": "bill_no", "customer": "cust_name",
                "amount": "amt", "status": "state"},
    "data_path": "data.list", "page_param": "page", "size_param": "size",
    "page_size": 2, "max_pages": 10,
}


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def request(self, method, url, headers=None, params=None, json=None):
        self.calls.append({"url": url, "params": params})
        return _FakeResp(self.pages.pop(0) if self.pages else {"data": {"list": []}})


class _FakeHttpFactory:
    def __init__(self, pages):
        self.client = _FakeClient(pages)

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


async def _create_connector(client, headers, company_id, name="erp-orders",
                            config=None, kind="rest"):
    r = await client.post(
        f"/connectors?company_id={company_id}", headers=headers,
        json={"name": name, "display_name": "ERP 订单同步",
              "description": "拉取销售订单", "kind": kind,
              "entity_type": "order", "config": config or REST_CONFIG})
    assert r.status_code == 201, r.text
    return r.json()["connector"]


async def _records(company_id, name):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(CanonicalRecord).where(
                CanonicalRecord.company_id == company_id))).scalars().all()
        return [r for r in rows if r.external_id], rows


async def test_rest_sync_upserts_and_watermarks(client, tenants, monkeypatch):
    co = tenants.alice_co["id"]
    conn = await _create_connector(client, tenants.alice["headers"], co)

    page1 = {"data": {"list": [
        {"bill_no": "SO-001", "cust_name": "华东医院", "amt": 12000,
         "state": "paid", "modified_time": "2026-11-01 10:00:00"},
        {"bill_no": "SO-002", "cust_name": "连锁药店", "amt": 3500,
         "state": "open", "modified_time": "2026-11-02 09:00:00"},
    ]}}
    page2 = {"data": {"list": [
        {"bill_no": "SO-003", "cust_name": "私人诊所", "amt": 800,
         "state": "paid", "modified_time": "2026-11-03 09:00:00"},
        # SO-002 在源系统被改：同步幂等更新而不是插入第二行
        {"bill_no": "SO-002", "cust_name": "连锁药店(已付款)", "amt": 3500,
         "state": "paid", "modified_time": "2026-11-04 09:00:00"},
    ]}}
    factory = _FakeHttpFactory([page1, page2])
    monkeypatch.setattr("app.core.http_client.http_client", factory)
    # erp.example.com 不真实解析：闸门有专门用例，本例钉映射/幂等/水位
    monkeypatch.setattr("app.toolbus.dispatch._assert_public_http_url",
                        lambda *a, **k: None)

    r = await client.post(
        f"/connectors/{conn['id']}/sync?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()["run"]
    assert body["status"] == "success" and body["records_upserted"] == 4
    assert body["watermark"] == "2026-11-04 09:00:00"

    ids, rows = await _records(co, "erp-orders")
    assert len(rows) == 3  # SO-002 幂等更新
    so2 = next(r for r in rows if r.external_id == "SO-002")
    assert so2.payload["customer"] == "连锁药店(已付款)"
    assert so2.payload["amount"] == 3500
    # 水位持久化回 config
    async with AsyncSessionLocal() as db:
        from app.models.toolbus import ToolRegistry
        row = await db.get(ToolRegistry, conn["id"])
        assert row.config["watermark"] == "2026-11-04 09:00:00"


async def test_connector_rejects_private_endpoint(client, tenants):
    co = tenants.alice_co["id"]
    bad = {**REST_CONFIG, "endpoint": "http://192.168.1.5"}
    r = await client.post(
        f"/connectors?company_id={co}", headers=tenants.alice["headers"],
        json={"name": "evil-sync", "kind": "rest", "entity_type": "order",
              "config": bad})
    assert r.status_code == 400 and "拒绝" in r.text


async def test_webhook_ingest_with_signature(client, tenants):
    import hashlib
    import hmac as _hmac
    import uuid as _uuid

    from app.models.company_secret import CompanySecret
    from app.services.secret_vault import encrypt

    co = tenants.alice_co["id"]
    secret_name = f"wh-key-{_uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        db.add(CompanySecret(company_id=co, name=secret_name,
                             ciphertext=encrypt("topsecret")))
        await db.commit()

    config = {"kind": "webhook", "entity_type": "contact",
              "id_field": "lead_id",
              "mapping": {"name": "cust_name", "status": "state"},
              "webhook_secret_name": secret_name}
    conn = await _create_connector(client, tenants.alice["headers"], co,
                                   name="crm-webhook", config=config,
                                   kind="webhook")
    payload = [{"lead_id": "L-1", "cust_name": "新客户A", "amt": None,
                "state": "new"},
               {"lead_id": "L-1", "cust_name": "新客户A改", "amt": None,
                "state": "qualified"}]
    body = json.dumps(payload, ensure_ascii=False).encode()
    sig = _hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    r = await client.post(
        f"/connectors/{conn['id']}/ingest?company_id={co}&signature={sig}",
        headers=tenants.alice["headers"], json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["upserted"] == 2  # 同 id 幂等 upsert
    ids, rows = await _records(co, "crm-webhook")
    assert len(rows) == 1 and rows[0].payload["name"] == "新客户A改"
    assert rows[0].payload["status"] == "qualified"

    # 坏签名 403
    r = await client.post(
        f"/connectors/{conn['id']}/ingest?company_id={co}&signature=bad",
        headers=tenants.alice["headers"], json=payload)
    assert r.status_code == 403


async def test_metrics_define_evaluate_and_bus_tool(client, tenants):
    co = tenants.alice_co["id"]
    conn = await _create_connector(client, tenants.alice["headers"], co)
    # 手工投喂三张订单（走 webhook 免签名路径需要 webhook kind；这里直接造行）
    from app.connectors.base import PullRecord, upsert_records
    from datetime import datetime
    recs = [
        PullRecord(external_id="M-1", payload={"amount": 100, "region": "华东",
                                               "status": "paid"}),
        PullRecord(external_id="M-2", payload={"amount": 250, "region": "华东",
                                               "status": "paid"}),
        PullRecord(external_id="M-3", payload={"amount": 60, "region": "华南",
                                               "status": "open"}),
    ]
    async with AsyncSessionLocal() as db:
        n = await upsert_records(db, company_id=co, source=conn["id"],
                                 entity_type="order", records=recs)
        await db.commit()
    assert n == 3

    # 定义两个指标：总销售额（sum）+ 已付订单数（count + filter）
    r = await client.post(
        f"/metrics?company_id={co}", headers=tenants.alice["headers"],
        json={"key": "total_sales", "name": "总销售额", "unit": "元",
              "spec": {"entity": "order", "op": "sum", "field": "payload.amount"}})
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/metrics?company_id={co}", headers=tenants.alice["headers"],
        json={"key": "paid_orders", "name": "已付订单数", "unit": "个",
              "spec": {"entity": "order", "op": "count",
                       "filters": [{"field": "payload.status",
                                    "op": "eq", "value": "paid"}]}})
    assert r.status_code == 201
    # 非法口径被拒
    r = await client.post(
        f"/metrics?company_id={co}", headers=tenants.alice["headers"],
        json={"key": "bad_metric", "name": "坏指标",
              "spec": {"entity": "order", "op": "drop"}})
    assert r.status_code == 400

    r = await client.post(
        f"/metrics/total_sales/evaluate?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200 and r.json()["value"] == 410
    r = await client.post(
        f"/metrics/paid_orders/evaluate?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.json()["value"] == 2

    # 员工经总线取数（同一口径、同一事实源）
    res = await execute_bus_tool("metrics_query", {"key": "total_sales"},
                                 company_id=co, agent_id="a-1")
    assert "error" not in res and res["value"] == 410
    res = await execute_bus_tool("metrics_query", {"key": "no_such"},
                                 company_id=co)
    assert "error" in res and "指标中心" in res["hint"]

    # 租户隔离：bob 的公司看不到 alice 的指标
    r = await client.post(
        f"/metrics/total_sales/evaluate?company_id={tenants.bob_co['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 404
    # bob 也看不到 alice 的规整层记录
    r = await client.get(
        f"/connectors/{conn['id']}/records?company_id={tenants.bob_co['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_aieos_query_metric_via_mcp(client, tenants):
    co = tenants.alice_co["id"]
    r = await client.post(
        f"/metrics?company_id={co}", headers=tenants.alice["headers"],
        json={"key": "paid_orders", "name": "已付订单数",
              "spec": {"entity": "order", "op": "count"}})
    assert r.status_code == 201

    # 出站 MCP Server 对外暴露 aieos.query_metric（7+1 个工具）
    r = await client.get("/mcp", headers=tenants.alice["headers"])
    names = {t["name"] for t in r.json()["tools"]}
    assert "aieos.query_metric" in names

    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "aieos.query_metric",
                      "arguments": {"key": "paid_orders"}}}
    r = await client.post("/mcp", json=rpc,
                          headers={**tenants.alice["headers"],
                                   "X-AI-EOS-Company": co})
    assert r.status_code == 200
    result_text = r.json()["result"]["content"][0]["text"]
    assert json.loads(result_text)["value"] == 0  # 空表 count=0
