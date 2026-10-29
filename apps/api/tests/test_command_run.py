"""一句话直达（`POST /command/run`）：老板说一句，公司自己干完。

与 `/command/plan` + `/command/execute` 两步式的区别就在这里：
不需要中间确认，一句话同时完成「理解」与「开工」。
"""
from __future__ import annotations

import json

import app.background_jobs as background_jobs
from app.db.session import AsyncSessionLocal
from sqlalchemy import select
from app.model_gateway import ModelGateway
from app.models.task import Task, TaskStatus


async def _agent(client, user, company_id, name="分析师") -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "市场分析"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _fake_model_id(_db) -> str:
    return "m1"


def _fake_plan(path: str, **extra):
    async def _chat(self, model_ids, messages, tools=None, config=None):
        payload = {"path": path, "reason": "测试",
                   "targets": [{"agent_id": 1, "task": "调研并出报告"}],
                   "team_goal": "做一份季度经营分析", "est_minutes": 20}
        payload.update(extra)
        return {"content": json.dumps(payload, ensure_ascii=False)}
    return _chat


async def test_run_direct_creates_and_starts_tasks(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _agent(client, alice, co["id"])
    monkeypatch.setattr("app.command_engine._first_model_id", _fake_model_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_plan("DIRECT"))
    submitted: list = []
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: submitted.append(fn) or "future")

    r = await client.post(f"/command/run?company_id={co['id']}", json={
        "phrase": "帮我做一份季度经营分析"}, headers=alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan"]["path"] == "DIRECT"
    assert body["plan"]["targets"][0]["agent_id"] == agent["id"]
    assert body["path"] == "DIRECT"
    assert body["jobs"][0]["created_count"] == 1
    assert submitted, "一句话直达必须真的开工，而不是只建待办"

    async with AsyncSessionLocal() as s:
        tasks = (await s.execute(select(Task))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.RUNNING
    assert tasks[0].assigned_agent_id == agent["id"]


async def test_run_project_returns_session(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    await _agent(client, alice, co["id"])
    monkeypatch.setattr("app.command_engine._first_model_id", _fake_model_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_plan("PROJECT"))
    kicked: list = []
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: kicked.append(fn) or "future")

    r = await client.post(f"/command/run?company_id={co['id']}", json={
        "phrase": "开一条新业务线"}, headers=alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["path"] == "PROJECT"
    assert body["session_id"]
    assert body["project_id"]
    assert kicked, "作战室 kickoff 必须被投递"


async def test_run_plan_defaults_to_project_on_garbage(client, tenants, monkeypatch):
    """模型返回非 JSON 时不能 500，要退到可执行的默认路径。"""
    alice, co = tenants.alice, tenants.alice_co
    await _agent(client, alice, co["id"])

    async def _junk(self, model_ids, messages, tools=None, config=None):
        return {"content": "我觉得可以干"}

    monkeypatch.setattr("app.command_engine._first_model_id", _fake_model_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _junk)
    monkeypatch.setattr(background_jobs, "submit_async", lambda *a, **kw: "future")
    r = await client.post(f"/command/run?company_id={co['id']}", json={
        "phrase": "随便干点啥"}, headers=alice["headers"])
    assert r.status_code == 201, r.text
    assert r.json()["plan"]["path"] == "PROJECT"


async def test_report_templates_endpoint(client, tenants):
    r = await client.get("/command/report-templates",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list) and data
    ids = {t["id"] for t in data}
    assert {"industry_research", "business_analysis"} <= ids
    for t in data:
        assert t.get("name") and t.get("sections")


async def test_run_rejects_foreign_company(client, tenants):
    r = await client.post(f"/command/run?company_id={tenants.bob_co['id']}", json={
        "phrase": "看别人公司的数据"}, headers=tenants.alice["headers"])
    assert r.status_code == 404
