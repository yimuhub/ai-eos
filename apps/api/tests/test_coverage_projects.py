"""覆盖率补齐：项目 CRUD、里程碑、成员、生命周期、进度、日志与 AI 助手。

不依赖 LLM：chat-operation 只走规则匹配；模型相关的执行路径不在本文件。
"""
from __future__ import annotations

import uuid

import pytest

from app.api.routes.assets import storage_root
from app.db.session import AsyncSessionLocal
from app.models.asset import Asset, AssetType

pytestmark = pytest.mark.asyncio


async def _asset(company_id: str, user_id: str, name: str, content: str,
                 project_id: str | None = None) -> str:
    root = storage_root().resolve()
    fname = f"{uuid.uuid4().hex[:8]}_{name}"
    fpath = root / "artifacts" / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content, encoding="utf-8")
    async with AsyncSessionLocal() as s:
        a = Asset(company_id=company_id, name=name, type=AssetType.REPORT,
                  path=str(fpath.relative_to(root)), size=len(content.encode()),
                  created_by=user_id, creator_type="user", project_id=project_id)
        s.add(a)
        await s.commit()
        return a.id


async def _project(client, user, company_id: str, **extra) -> dict:
    body = {"name": f"项目-{uuid.uuid4().hex[:5]}", "company_id": company_id}
    body.update(extra)
    r = await client.post("/projects", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _agent(client, user, company_id: str, **extra) -> dict:
    body = {"company_id": company_id, "name": f"员工-{uuid.uuid4().hex[:6]}"}
    body.update(extra)
    r = await client.post("/agents", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def test_project_crud_and_status_transitions(client, make_user,
                                                   make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    p = await _project(client, user, co["id"], description="立项说明")
    assert p["status"] == "PLANNING"

    r = await client.get(f"/projects?company_id={co['id']}", headers=h)
    assert r.status_code == 200 and any(x["id"] == p["id"] for x in r.json())
    assert r.json()[0]["progress"] == 0
    r = await client.get("/projects", headers=h)
    assert r.status_code == 200 and any(x["id"] == p["id"] for x in r.json())

    r = await client.get(f"/projects/{p['id']}", headers=h)
    assert r.status_code == 200 and r.json()["milestones"] == []

    r = await client.patch(f"/projects/{p['id']}", json={
        "name": "改名项目", "status": "ACTIVE"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE"

    # 非法状态迁移：ACTIVE -> PLANNING
    r = await client.patch(f"/projects/{p['id']}", json={"status": "PLANNING"},
                           headers=h)
    assert r.status_code == 409
    r = await client.patch(f"/projects/{p['id']}", json={"status": "BOGUS"},
                           headers=h)
    assert r.status_code == 400

    r = await client.post("/projects", json={"name": "无公司"}, headers=h)
    assert r.status_code == 422


async def test_project_milestones_and_progress(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p = await _project(client, user, co["id"])

    r = await client.post(f"/projects/{p['id']}/milestones", json={
        "name": "阶段一", "target_date": "2026-01-01"}, headers=h)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["name"] == "阶段一"

    r = await client.patch(f"/milestones/{m['id']}", json={
        "status": "COMPLETED", "description": "已验收"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["completed_at"]

    r = await client.get(f"/projects/{p['id']}/progress", headers=h)
    body = r.json()
    assert body["milestones_total"] == 1 and body["milestones_done"] == 1

    # 里程碑不属于任何项目时 404
    r = await client.patch(f"/milestones/{uuid.uuid4().hex}", json={"name": "x"},
                           headers=h)
    assert r.status_code == 404


async def test_project_members_add_remove(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p = await _project(client, user, co["id"])
    a = await _agent(client, user, co["id"])

    r = await client.post(f"/projects/{p['id']}/members", json={
        "member_type": "agent", "member_id": a["id"],
        "role_in_project": "开发"}, headers=h)
    assert r.status_code == 201, r.text
    # 幂等
    r = await client.post(f"/projects/{p['id']}/members", json={
        "member_type": "agent", "member_id": a["id"]}, headers=h)
    assert r.status_code == 201 and r.json()["detail"] == "already a member"

    # user 成员不校验 Agent 存在性
    r = await client.post(f"/projects/{p['id']}/members", json={
        "member_type": "user", "member_id": user["id"]}, headers=h)
    assert r.status_code == 201

    r = await client.get(f"/projects/{p['id']}", headers=h)
    assert len(r.json()["members"]) == 2

    r = await client.delete(f"/projects/{p['id']}/members/{a['id']}", headers=h)
    assert r.status_code == 204
    r = await client.get(f"/projects/{p['id']}", headers=h)
    assert len(r.json()["members"]) == 1

    # 不存在的 agent
    r = await client.post(f"/projects/{p['id']}/members", json={
        "member_type": "agent", "member_id": uuid.uuid4().hex}, headers=h)
    assert r.status_code == 404


async def test_project_lifecycle_pause_resume_stop_and_delete(client, make_user,
                                                              make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p = await _project(client, user, co["id"])
    await client.patch(f"/projects/{p['id']}", json={"status": "ACTIVE"},
                       headers=h)

    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "pause"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "PAUSED"
    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "pause"}, headers=h)
    assert r.status_code == 409

    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "resume"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE"

    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "stop"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "stop"}, headers=h)
    assert r.status_code == 409

    r = await client.post(f"/projects/{p['id']}/lifecycle",
                          json={"action": "explode"}, headers=h)
    assert r.status_code == 400

    # delete：彻底清除
    p2 = await _project(client, user, co["id"])
    r = await client.post(f"/projects/{p2['id']}/lifecycle",
                          json={"action": "delete"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.get(f"/projects/{p2['id']}", headers=h)
    assert r.status_code == 404


async def test_project_batch_delete_and_delete_endpoint(client, make_user,
                                                        make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p1 = await _project(client, user, co["id"])
    p2 = await _project(client, user, co["id"])

    r = await client.delete(f"/projects/{p1['id']}", headers=h)
    assert r.status_code == 204

    r = await client.post("/projects/batch-delete", json={
        "project_ids": [p2["id"], "missing"]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_count"] == 1
    assert body["failed"][0]["id"] == "missing"


async def test_project_chat_operation_manual_and_nlp(client, make_user,
                                                     make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p = await _project(client, user, co["id"])
    a = await _agent(client, user, co["id"], name="战略分析师")
    r = await client.post(f"/departments?cid={co['id']}",
                          json={"name": "市场部"}, headers=h)
    dept = r.json()["id"]
    await _agent(client, user, co["id"], name="市场专员", department_id=dept)

    # 手动模式
    r = await client.post(f"/projects/{p['id']}/chat-operation", json={
        "message": "手动指派", "agent_ids": [a["id"]],
        "department_ids": [dept]}, headers=h)
    assert r.status_code == 200, r.text
    kinds = {x["type"] for x in r.json()["actions"]}
    assert {"assign_agent", "assign_department"} <= kinds

    r = await client.get(f"/projects/{p['id']}", headers=h)
    assert r.json()["manager_agent_id"] == a["id"]

    # NLP：按名字 + 关键词命中负责人
    p2 = await _project(client, user, co["id"])
    r = await client.post(f"/projects/{p2['id']}/chat-operation", json={
        "message": f"让战略分析师来负责这个项目"}, headers=h)
    assert r.status_code == 200
    assert any(x["type"] == "assign_agent" for x in r.json()["actions"])

    # 部门承接
    r = await client.post(f"/projects/{p2['id']}/chat-operation", json={
        "message": "让市场部整个团队承接"}, headers=h)
    assert r.status_code == 200
    assert any(x["type"] == "assign_department" for x in r.json()["actions"])

    # 无法理解
    r = await client.post(f"/projects/{p2['id']}/chat-operation", json={
        "message": "今天天气不错"}, headers=h)
    assert r.status_code == 200 and r.json()["actions"] == []


async def test_project_deliverables_logs_and_export(client, make_user,
                                                    make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    p = await _project(client, user, co["id"])
    await _asset(co["id"], user["id"], "方案.md", "# 方案", project_id=p["id"])

    r = await client.get(f"/projects/{p['id']}/deliverables", headers=h)
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["name"] == "方案.md"

    r = await client.get(f"/projects/{p['id']}/logs", headers=h)
    assert r.status_code == 200 and "count" in r.json()

    r = await client.get(f"/projects/{p['id']}/logs/export", headers=h)
    assert r.status_code == 200
    assert "项目日志" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")
