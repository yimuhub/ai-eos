"""覆盖率补齐：Agent CRUD、能力包、上下级、批量操作、模型绑定与配额闸门。

全部走真实 ASGI + 临时 SQLite，不 mock 鉴权；只有"反思"用 monkeypatch
替换掉会调模型的实现（本文件关注路由分支，不关注 LLM）。
"""
from __future__ import annotations

import uuid

import pytest

from app.db.session import AsyncSessionLocal
from app.models.company import Department
from app.models.membership import CompanyMember

pytestmark = pytest.mark.asyncio


async def _agent(client, user, company_id: str, **extra) -> dict:
    body = {"company_id": company_id, "name": f"员工-{uuid.uuid4().hex[:6]}"}
    body.update(extra)
    r = await client.post("/agents", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def test_agent_crud_and_capability_packs(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    r = await client.get("/agents/capability-packs", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["packs"] and r.json()["backends"]

    a = await _agent(client, user, co["id"], role="分析师",
                     capabilities=["web_browsing"], backend="native_llm",
                     description="负责调研", visibility="company")
    assert a["visibility"] == "company"

    # card 包含身份与 needs_auth 协议字段
    r = await client.get(f"/agents/{a['id']}/card", headers=h)
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["agent_id"] == a["id"] and card["card_version"] == "1.0"
    assert "needs_auth" in card

    # 列表 + 过滤
    r = await client.get(f"/agents?company_id={co['id']}", headers=h)
    assert r.status_code == 200 and any(x["id"] == a["id"] for x in r.json())
    r = await client.get(f"/agents?department_id={uuid.uuid4().hex}", headers=h)
    assert r.status_code == 200 and r.json() == []

    # 更新：能力包清空 + 非法状态
    r = await client.patch(f"/agents/{a['id']}", json={
        "name": "首席分析师", "capabilities": [], "backend": "native_llm",
        "visibility": "private"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "首席分析师"
    assert r.json()["visibility"] == "private"

    r = await client.patch(f"/agents/{a['id']}", json={"status": "NOT_A_STATUS"},
                           headers=h)
    assert r.status_code == 422

    # 非法可见性
    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "x", "visibility": "everyone"},
        headers=h)
    assert r.status_code == 422

    # 详情 / 删除
    r = await client.get(f"/agents/{a['id']}", headers=h)
    assert r.status_code == 200
    r = await client.delete(f"/agents/{a['id']}", headers=h)
    assert r.status_code == 204
    r = await client.get(f"/agents/{a['id']}", headers=h)
    assert r.status_code == 404


async def test_agent_manager_tree_and_batch_assign(client, make_user,
                                                   make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    boss = await _agent(client, user, co["id"], name="总监")
    dev = await _agent(client, user, co["id"], name="开发")

    r = await client.put(f"/agents/{dev['id']}/manager",
                         json={"manager_agent_id": boss["id"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["manager_agent_id"] == boss["id"]

    r = await client.get(f"/agents/{boss['id']}/subordinates", headers=h)
    assert [x["id"] for x in r.json()] == [dev["id"]]

    # 自我为上级 / 成环
    r = await client.put(f"/agents/{dev['id']}/manager",
                         json={"manager_agent_id": dev["id"]}, headers=h)
    assert r.status_code == 400
    r = await client.put(f"/agents/{boss['id']}/manager",
                         json={"manager_agent_id": dev["id"]}, headers=h)
    assert r.status_code == 400
    assert "循环" in r.text

    # 批量指派：含一个不存在的 id
    r = await client.post("/agents/batch-assign-manager", json={"assignments": [
        {"agent_id": dev["id"], "manager_agent_id": None},
        {"agent_id": "missing", "manager_agent_id": None},
    ]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1
    assert r.json()["failed"][0]["agent_id"] == "missing"

    # 清除上级
    r = await client.put(f"/agents/{dev['id']}/manager",
                         json={"manager_agent_id": None}, headers=h)
    assert r.status_code == 200 and r.json()["manager_agent_id"] is None


async def test_agent_batch_create_and_delete(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    r = await client.post("/agents/batch-create", json={
        "company_id": co["id"],
        "agents": [{"name": "甲", "role": "运营"}, {"name": "  "},
                   {"name": "乙", "capabilities": ["scheduled_work"]}],
    }, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_count"] == 2

    # 批量创建不会像单个创建那样自动生成周期自动化，列表接口仍应可用
    r = await client.get(f"/automations?company_id={co['id']}", headers=h)
    assert r.status_code == 200, r.text

    ids = [x["id"] for x in body["created"]]
    r = await client.post("/agents/batch-delete", json={
        "agent_ids": [ids[0], "missing"]}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["deleted_count"] == 1 and res["deleted"] == [ids[0]]
    assert res["failed"][0]["id"] == "missing"

    # 单个删除后再批量删同一 id：应报不存在而不是 500
    r = await client.post("/agents/batch-delete", json={"agent_ids": [ids[0]]},
                         headers=h)
    assert r.status_code == 200 and r.json()["deleted_count"] == 0


async def test_agent_model_bind_switch_list(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    a = await _agent(client, user, co["id"])
    model = await _model(client, user, co["id"])

    r = await client.post(f"/agents/{a['id']}/models", json={
        "role": "primary", "model_id": model}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(f"/agents/{a['id']}/models", json={
        "role": "fallback", "model_id": model}, headers=h)
    assert r.status_code == 201

    r = await client.get(f"/agents/{a['id']}/models", headers=h)
    assert {x["role"] for x in r.json()} == {"primary", "fallback"}

    r = await client.put(f"/agents/{a['id']}/switch-model",
                         json={"model_id": model}, headers=h)
    assert r.status_code == 200 and r.json()["role"] == "primary"
    r = await client.get(f"/agents/{a['id']}/models", headers=h)
    assert len(r.json()) == 2  # fallback 保留，primary 被替换而不是叠加


async def _model(client, user, company_id: str, **extra) -> str:
    body = {"name": f"模型-{uuid.uuid4().hex[:5]}", "provider_type": "openai_compat",
            "model_id": "gpt-test", "company_id": company_id}
    body.update(extra)
    r = await client.post("/models", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_agent_quota_gate_returns_409(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            __import__("sqlalchemy").select(CompanyMember).where(
                CompanyMember.company_id == co["id"],
                CompanyMember.user_id == user["id"]))).scalars().first()
        if m is None:
            m = CompanyMember(company_id=co["id"], user_id=user["id"],
                              role="owner", status="active")
            s.add(m)
        m.agent_quota = 1
        await s.commit()

    await _agent(client, user, co["id"])
    r = await client.post("/agents", json={"company_id": co["id"], "name": "超额"},
                          headers=h)
    assert r.status_code == 409, r.text
    assert "配额" in r.text

    # 审批单必须已经落库（409 后仍可查询）
    r = await client.get(f"/intervention/requests?company_id={co['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert any(x["action_type"] == "agent_quota" for x in r.json())


async def test_agent_department_visibility_and_dept_access(client, make_user,
                                                           make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    r = await client.post(f"/departments?cid={co['id']}",
                          json={"name": "市场部"}, headers=h)
    assert r.status_code == 201, r.text
    dept = r.json()["id"]

    a = await _agent(client, user, co["id"], department_id=dept,
                     visibility="department")
    assert a["department_id"] == dept

    # 部门配额：直写部门字段，验证「目标部门」优先级
    async with AsyncSessionLocal() as s:
        d = await s.get(Department, dept)
        d.agent_quota = 1
        await s.commit()
    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "第二个", "department_id": dept},
        headers=h)
    assert r.status_code == 409, r.text


async def test_agent_reflect_endpoint_uses_engine(client, make_user,
                                                  make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    a = await _agent(client, user, co["id"])

    import app.reflection_engine as refl

    async def fake_reflect(db, agent):
        return {"reflected": True, "memories_written": 0}

    monkeypatch.setattr(refl, "self_reflect", fake_reflect)
    r = await client.post(f"/agents/{a['id']}/reflect", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["reflected"] is True
