"""覆盖率补齐：任务中心全生命周期、人工介入、模板沉淀与轨迹查询。

后台执行一律用 monkeypatch 拦住（`_dispatch_or_revert`），本文件只验证
路由与状态机分支，不启动真实 worker、不发模型请求。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from app.api.routes import tasks as tasks_route
from app.db.session import AsyncSessionLocal
from app.models.task import Task, TaskStatus

pytestmark = pytest.mark.asyncio


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake(db, task_id, agent_id, company_id=None):
        calls.append((task_id, agent_id))

    monkeypatch.setattr(tasks_route, "_dispatch_or_revert", _fake)
    return calls


async def _agent(client, user, company_id: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": f"员工-{uuid.uuid4().hex[:6]}"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _task(client, user, company_id: str, **extra) -> dict:
    body = {"company_id": company_id, "title": f"任务-{uuid.uuid4().hex[:5]}"}
    body.update(extra)
    r = await client.post("/tasks", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _set_status(task_id: str, status: TaskStatus) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(update(Task).where(Task.id == task_id)
                        .values(status=status))
        await s.commit()


async def test_task_create_list_get_summary_and_validation(client, make_user,
                                                           make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    aid = await _agent(client, user, co["id"])

    t = await _task(client, user, co["id"], description="做调研",
                    priority="HIGH", assigned_agent_id=aid)
    assert t["status"] == "TODO" and t["priority"] == "HIGH"

    r = await client.get(f"/tasks?company_id={co['id']}&status=TODO&agent_id={aid}",
                         headers=h)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [t["id"]]
    # 不带 company_id 时跨公司聚合
    r = await client.get("/tasks", headers=h)
    assert r.status_code == 200 and any(x["id"] == t["id"] for x in r.json())

    r = await client.get(f"/tasks/{t['id']}", headers=h)
    assert r.status_code == 200 and r.json()["title"] == t["title"]
    r = await client.get(f"/tasks/{t['id']}/summary", headers=h)
    assert r.status_code == 200 and "result" not in r.json()
    assert r.json()["executions_total"] == 0
    r = await client.get(f"/tasks/{t['id']}/executions", headers=h)
    assert r.status_code == 200 and r.json() == []
    r = await client.get(f"/tasks/{t['id']}/trace", headers=h)
    assert r.status_code == 200 and r.json()["total"]["executions"] == 0
    r = await client.get(f"/tasks/{t['id']}/logs", headers=h)
    assert r.status_code == 200 and r.json()["count"] >= 1

    # 非法优先级 / 不存在
    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "x", "priority": "SUPER"}, headers=h)
    assert r.status_code == 400
    r = await client.get(f"/tasks/{uuid.uuid4().hex}", headers=h)
    assert r.status_code == 404


async def test_task_cross_tenant_agent_rejected(client, tenants):
    r = await client.post("/tasks", json={
        "company_id": tenants.alice_co["id"], "title": "越权",
        "assigned_agent_id": await _agent(client, tenants.bob,
                                          tenants.bob_co["id"])},
        headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text


async def test_task_patch_and_interventions(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    a1 = await _agent(client, user, co["id"])
    a2 = await _agent(client, user, co["id"])
    t = await _task(client, user, co["id"], assigned_agent_id=a1)

    r = await client.patch(f"/tasks/{t['id']}", json={
        "priority": "URGENT", "description": "改需求",
        "assigned_agent_id": a2}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == "URGENT"
    assert r.json()["assigned_agent_id"] == a2

    r = await client.patch(f"/tasks/{t['id']}", json={"priority": "NOPE"},
                           headers=h)
    assert r.status_code == 400

    # 人工介入：改优先级 + 换负责人
    r = await client.post(f"/tasks/{t['id']}/interventions", json={
        "type": "PRIORITY_CHANGE", "content": "先做这个",
        "new_priority": "HIGH"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["applied"]["priority"] == "HIGH"

    r = await client.post(f"/tasks/{t['id']}/interventions", json={
        "type": "AGENT_CHANGE", "content": "换人",
        "new_agent_id": a1}, headers=h)
    assert r.status_code == 201 and r.json()["applied"]["assigned_agent_id"] == a1

    r = await client.post(f"/tasks/{t['id']}/interventions", json={
        "type": "MESSAGE", "content": "普通留言"}, headers=h)
    assert r.status_code == 201 and r.json()["applied"] == {}

    r = await client.post(f"/tasks/{t['id']}/interventions", json={
        "type": "PRIORITY_CHANGE", "new_priority": "BAD"}, headers=h)
    assert r.status_code == 400

    r = await client.get(f"/tasks/{t['id']}/logs", headers=h)
    assert any(x["kind"] == "intervention" for x in r.json()["logs"])


async def test_task_start_pause_resume_cancel(client, make_user, make_company,
                                              dispatched):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    aid = await _agent(client, user, co["id"])

    bare = await _task(client, user, co["id"])
    r = await client.post(f"/tasks/{bare['id']}/start", headers=h)
    assert r.status_code == 400, r.text

    t = await _task(client, user, co["id"], assigned_agent_id=aid)
    r = await client.post(f"/tasks/{t['id']}/start", headers=h)
    assert r.status_code == 200, r.text
    assert dispatched == [(t["id"], aid)]

    # 已 RUNNING，再次 start 被 CAS 拒绝
    r = await client.post(f"/tasks/{t['id']}/start", headers=h)
    assert r.status_code == 409

    r = await client.post(f"/tasks/{t['id']}/pause", headers=h)
    assert r.status_code == 200
    r = await client.post(f"/tasks/{t['id']}/resume", headers=h)
    assert r.status_code == 200
    # RUNNING 不可再次 resume
    r = await client.post(f"/tasks/{t['id']}/resume", headers=h)
    assert r.status_code == 409

    r = await client.post(f"/tasks/{t['id']}/cancel?reason=不做了", headers=h)
    assert r.status_code == 200
    r = await client.post(f"/tasks/{t['id']}/cancel", headers=h)
    assert r.status_code == 409


async def test_task_takeover_flow(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    aid = await _agent(client, user, co["id"])
    t = await _task(client, user, co["id"], assigned_agent_id=aid)
    await _set_status(t["id"], TaskStatus.RUNNING)

    r = await client.post(f"/tasks/{t['id']}/takeover",
                          json={"type": "INSTRUCTION", "content": "按新方向"},
                          headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["paused_executions"] == 0

    r = await client.post(f"/tasks/{t['id']}/takeover",
                          json={"type": "INSTRUCTION", "content": "再来一次"},
                          headers=h)
    assert r.status_code == 409

    r = await client.post(f"/tasks/{t['id']}/takeover/resolve",
                          json={"type": "INSTRUCTION", "content": "恢复吧"},
                          headers=h)
    assert r.status_code == 200, r.text

    # 已取消的任务不能接管（只有 RUNNING/PLANNING 可以）
    await client.post(f"/tasks/{t['id']}/cancel", headers=h)
    r = await client.post(f"/tasks/{t['id']}/takeover",
                          json={"type": "INSTRUCTION"}, headers=h)
    assert r.status_code == 409


async def test_task_retry_from_failed(client, make_user, make_company,
                                      dispatched):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    aid = await _agent(client, user, co["id"])
    t = await _task(client, user, co["id"], assigned_agent_id=aid)

    r = await client.post(f"/tasks/{t['id']}/retry", headers=h)
    assert r.status_code == 409

    await _set_status(t["id"], TaskStatus.FAILED)
    r = await client.post(f"/tasks/{t['id']}/retry", headers=h)
    assert r.status_code == 200, r.text
    assert dispatched == [(t["id"], aid)]


async def test_task_save_as_template_idempotent_and_conflict(client, make_user,
                                                             make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    t = await _task(client, user, co["id"], description="模板描述")

    r = await client.post(f"/tasks/{t['id']}/save-as-template", json={},
                          headers=h)
    assert r.status_code == 409, "未完成任务不能存为模板"

    await _set_status(t["id"], TaskStatus.COMPLETED)
    r = await client.post(f"/tasks/{t['id']}/save-as-template",
                          json={"name": "周报模板"}, headers=h)
    assert r.status_code == 200, r.text
    tpl = r.json()
    assert tpl["name"] == "周报模板"

    # 同名任务模板再次保存 → 返回已有行（幂等）
    r = await client.post(f"/tasks/{t['id']}/save-as-template",
                          json={"name": "周报模板"}, headers=h)
    assert r.status_code == 200 and r.json()["id"] == tpl["id"]

    # 同名普通自动化规则 → 409（语义混淆必须拒绝，而不是静默复用）
    r = await client.post(f"/automations?company_id={co['id']}", json={
        "name": "普通规则", "steps": [{"seq": 1, "type": "noop"}]},
        headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(f"/tasks/{t['id']}/save-as-template",
                          json={"name": "普通规则"}, headers=h)
    assert r.status_code == 409, r.text
