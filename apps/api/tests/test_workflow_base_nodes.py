"""工作流基座三节点（V6 M1-e）：HTTP / TOOL / TRANSFORM。

钉住的契约：
1. HTTP 节点：公网闸门（私网拒绝→节点失败）、非 2xx 不炸流程（结构化结果
   交给 CONDITION 分支）、响应体截断；
2. TOOL 节点：走工具总线执行（内置工具 monkeypatch 技能层验证贯通），
   未注册工具 → 执行失败且错误可读；
3. TRANSFORM 节点：模板取上游/入参、数组下标、取不到 → None（不静默串改）。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.workflow import RunStatus, WorkflowStep
from app.workflow_runtime import advance_execution, start_workflow


async def _create_workflow(client, headers, company_id, nodes, edges,
                           name="基座节点测试流", inputs=None):
    r = await client.post(
        f"/workflows?company_id={company_id}",
        json={"name": name, "nodes": nodes, "edges": edges,
              "inputs": inputs or []},
        headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run_and_finish(workflow_id: str, started_by: str,
                          input_data=None):
    """手动开跑并推进到终态（绕过后台 worker，断言确定性）。

    started_by 有 FK 约束，必须传真实用户 id。
    """
    async with AsyncSessionLocal() as s:
        ex = await start_workflow(s, workflow_id, started_by,
                                  input_data=input_data or {})
        eid = ex.id
    async with AsyncSessionLocal() as s:
        ex = await advance_execution(s, eid)
        return ex


async def _step_output(eid: str, node_key: str) -> dict:
    async with AsyncSessionLocal() as s:
        steps = (await s.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid,
            WorkflowStep.node_key == node_key))).scalars().all()
        return steps[-1].output_data or {}


# ---------------------------------------------------------------------------
# HTTP 节点
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def request(self, method, url, headers=None, content=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "content": content})
        return self._resp


class _FakeHttp:
    """替身 http_client：记录调用并返回预置响应。"""
    last_client = None

    def __init__(self, resp):
        _FakeHttp.last_client = _FakeClient(resp)

    async def __aenter__(self):
        return _FakeHttp.last_client

    async def __aexit__(self, *exc):
        return False


async def test_http_node_success_and_payload(client, tenants, monkeypatch):
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "h", "node_type": "HTTP", "name": "查汇率",
         "config": {"url": "https://api.example.com/rate",
                    "method": "POST", "headers": {"X-App": "aieos"},
                    "body": {"base": "CNY"}, "timeout_seconds": 10}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "h"},
        {"from_node": "h", "to_node": "e"}])

    resp = _FakeResp(200, json.dumps({"rate": 7.2}, ensure_ascii=False))
    monkeypatch.setattr("app.core.http_client.http_client",
                        lambda **kw: _FakeHttp(resp))
    # api.example.com 不真实解析：端点闸门有专门用例，本例钉载荷与方法
    monkeypatch.setattr("app.toolbus.dispatch._assert_public_http_url",
                        lambda *a, **k: None)
    ex = await _run_and_finish(wid, tenants.alice["id"])
    assert ex.status == RunStatus.COMPLETED
    out = await _step_output(ex.id, "h")
    assert out["ok"] is True and out["status"] == 200
    assert out["json"]["rate"] == 7.2
    call = _FakeHttp.last_client.calls[0]
    assert call["method"] == "POST"
    assert json.loads(call["content"]) == {"base": "CNY"}


async def test_http_node_non_2xx_does_not_fail_flow(client, tenants,
                                                    monkeypatch):
    """非 2xx 是业务结果不是基础设施故障：结构化返回，流程继续走。"""
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "h", "node_type": "HTTP", "name": "调接口",
         "config": {"url": "https://api.example.com/flaky"}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "h"},
        {"from_node": "h", "to_node": "e"}])
    monkeypatch.setattr("app.core.http_client.http_client",
                        lambda **kw: _FakeHttp(_FakeResp(503, "boom")))
    monkeypatch.setattr("app.toolbus.dispatch._assert_public_http_url",
                        lambda *a, **k: None)
    ex = await _run_and_finish(wid, tenants.alice["id"])
    assert ex.status == RunStatus.COMPLETED
    out = await _step_output(ex.id, "h")
    assert out["ok"] is False and out["status"] == 503


async def test_http_node_blocks_private_url(client, tenants):
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "h", "node_type": "HTTP", "name": "打内网",
         "config": {"url": "http://192.168.1.5/admin"}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "h"},
        {"from_node": "h", "to_node": "e"}])
    ex = await _run_and_finish(wid, tenants.alice["id"])
    assert ex.status == RunStatus.FAILED
    assert "拒绝" in (ex.error or "")


# ---------------------------------------------------------------------------
# TOOL 节点
# ---------------------------------------------------------------------------

async def test_tool_node_runs_builtin_via_bus(client, tenants, monkeypatch):
    """web_search 走总线：技能层被调用、结果进 step 输出、流程走完。"""
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "t", "node_type": "TOOL", "name": "搜索",
         "config": {"tool_name": "web_search",
                    "arguments": {"query": "ai-eos", "max_results": 3}}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "t"},
        {"from_node": "t", "to_node": "e"}])

    async def fake_skill(slug, payload):
        assert slug == "web_search"
        return {"result": {"results": [{"title": "AI-EOS 官网",
                                        "body": "数字员工平台"}]}}

    monkeypatch.setattr("app.skill_system.execute_skill", fake_skill)
    ex = await _run_and_finish(wid, tenants.alice["id"])
    assert ex.status == RunStatus.COMPLETED
    out = await _step_output(ex.id, "t")
    assert out["tool"] == "web_search"
    assert out["result"]["results"][0]["title"] == "AI-EOS 官网"


async def test_tool_node_unknown_tool_fails_flow(client, tenants):
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "t", "node_type": "TOOL", "name": "调不存在的工具",
         "config": {"tool_name": "no_such_tool", "arguments": {}}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "t"},
        {"from_node": "t", "to_node": "e"}])
    ex = await _run_and_finish(wid, tenants.alice["id"])
    assert ex.status == RunStatus.FAILED
    assert "未注册" in (ex.error or "")


# ---------------------------------------------------------------------------
# TRANSFORM 节点
# ---------------------------------------------------------------------------

async def test_transform_maps_input_and_upstream(client, tenants, monkeypatch):
    """上游 TOOL 输出 + 执行入参都能被映射；数组下标可用；流程走到 END。"""
    co = tenants.alice_co["id"]
    wid = await _create_workflow(client, tenants.alice["headers"], co, [
        {"node_key": "s", "node_type": "START", "name": "开始"},
        {"node_key": "t", "node_type": "TOOL", "name": "搜索",
         "config": {"tool_name": "web_search",
                    "arguments": {"query": "q"}}},
        {"node_key": "tr", "node_type": "TRANSFORM", "name": "整形",
         "config": {"set": {
             "company": "{{input.company_id}}",
             "first_title": "{{t.result.results.0.title}}",
             "engine": "ai-eos-v6",
             "missing": "{{t.result.results.9.title}}",
         }}},
        {"node_key": "e", "node_type": "END", "name": "结束"},
    ], [{"from_node": "s", "to_node": "t"},
        {"from_node": "t", "to_node": "tr"},
        {"from_node": "tr", "to_node": "e"}],
        inputs=[{"name": "company_id", "type": "text", "required": True}])

    async def fake_skill(slug, payload):
        return {"result": {"results": [{"title": "第一条"}]}}

    monkeypatch.setattr("app.skill_system.execute_skill", fake_skill)
    ex = await _run_and_finish(wid, tenants.alice["id"], input_data={"company_id": co})
    assert ex.status == RunStatus.COMPLETED
    out = await _step_output(ex.id, "tr")
    data = out["data"]
    assert data["company"] == co
    assert data["first_title"] == "第一条"
    assert data["engine"] == "ai-eos-v6"
    assert data["missing"] is None  # 取不到显式 None，不静默
