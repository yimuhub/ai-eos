"""阵容锁定（M3-B）：角色档案、从执行存阵容、spec 导入白名单。"""
import pytest

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.team import RoleProfile
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)


def spec_with_agents(*names: str) -> dict:
    nodes = [{"key": "start", "type": "START", "name": "开始"}]
    edges = []
    prev = "start"
    for i, name in enumerate(names, 1):
        key = f"n{i}"
        nodes.append({"key": key, "type": "AGENT", "name": f"第{i}步",
                      "agent": name, "task": f"做第{i}步"})
        edges.append({"from": prev, "to": key})
        prev = key
    nodes.append({"key": "end", "type": "END", "name": "结束"})
    edges.append({"from": prev, "to": "end"})
    return {"spec": "v1", "key": "cast-test", "name": "阵容测试",
            "nodes": nodes, "edges": edges}


async def _company(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    return user, co


@pytest.mark.asyncio
async def test_role_profile_crud(client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    h = user["headers"]
    base = f"?company_id={co['id']}"

    r = await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "finance-analyst", "name": "财务分析师",
        "domain": "财务", "capability_tags": ["预算", "现金流"],
        "default_gate": {"assert": {"min_chars": 500}}})
    assert r.status_code == 201
    body = r.json()
    assert body["key"] == "finance-analyst"
    assert body["name"] == "财务分析师"

    assert (await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "finance-analyst", "name": "重复"})).status_code == 409
    assert (await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "中文Key", "name": "x"})).status_code == 400

    rows = (await client.get(f"/role-profiles{base}", headers=h)).json()
    assert [r["key"] for r in rows] == ["finance-analyst"]


@pytest.mark.asyncio
async def test_save_loadout_requires_existing_roles(client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    h = user["headers"]
    base = f"?company_id={co['id']}"

    await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "pm", "name": "产品经理"})
    r = await client.post(f"/teams{base}", headers=h, json={
        "name": "标准三人组", "role_keys": ["pm", "ghost"]})
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"]

    r = await client.post(f"/teams{base}", headers=h, json={
        "name": "标准组", "role_keys": ["pm"],
        "default_inputs": {"audience": "泛职场"}})
    assert r.status_code == 201
    t = r.json()
    assert t["roles"] == [{"key": "pm", "name": "产品经理"}]
    assert "agent_id" not in t["roles"][0]      # 存角色 key，不存员工 id


@pytest.mark.asyncio
async def test_save_loadout_from_execution_creates_profiles(
        client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    h = user["headers"]
    async with AsyncSessionLocal() as db:
        wf = WorkflowDefinition(company_id=co["id"], name="抽阵容",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        a1 = Agent(company_id=co["id"], name="产品经理")
        a2 = Agent(company_id=co["id"], name="数据分析师")
        db.add_all([a1, a2])
        await db.flush()
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="start",
                         node_type=NodeType.START, name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="p",
                         node_type=NodeType.AGENT, name="规划",
                         config={"agent_id": a1.id}),
            WorkflowNode(workflow_id=wf.id, node_key="d",
                         node_type=NodeType.AGENT, name="分析",
                         config={"agent_id": a2.id}),
            WorkflowNode(workflow_id=wf.id, node_key="end",
                         node_type=NodeType.END, name="结束"),
        ])
        for f, t in [("start", "p"), ("p", "d"), ("d", "end")]:
            db.add(WorkflowEdge(workflow_id=wf.id, from_node=f, to_node=t))
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.COMPLETED,
                               started_by=user["id"], current_nodes=["end"],
                               input_data={})
        db.add(ex)
        await db.flush()
        for key in ("start", "p", "d", "end"):
            db.add(WorkflowStep(execution_id=ex.id, node_key=key,
                                node_type=NodeType.AGENT if key in ("p", "d")
                                else NodeType.START,
                                status=RunStatus.COMPLETED, attempt=1,
                                output_data={"output": f"{key}-ok"}))
        await db.commit()
        eid = ex.id
        a1_id, a2_id = a1.id, a2.id

    r = await client.post(f"/teams/from-execution?company_id={co['id']}",
                          headers=h, json={"execution_id": eid, "name": "抽出来的阵容"})
    assert r.status_code == 201
    roles = r.json()["roles"]
    assert [x["name"] for x in roles] == ["产品经理", "数据分析师"]
    assert all(x.get("key") for x in roles)

    async with AsyncSessionLocal() as db:
        for aid, name in [(a1_id, "产品经理"), (a2_id, "数据分析师")]:
            a = await db.get(Agent, aid)
            assert a.role_profile_id
            p = await db.get(RoleProfile, a.role_profile_id)
            assert p.name == name and p.source == "from_execution"


@pytest.mark.asyncio
async def test_import_with_loadout_rejects_outside_role(
        client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    h = user["headers"]
    base = f"?company_id={co['id']}"
    async with AsyncSessionLocal() as db:
        pm = Agent(company_id=co["id"], name="产品经理")
        other = Agent(company_id=co["id"], name="外部顾问")
        db.add_all([pm, other])
        await db.commit()

    await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "pm", "name": "产品经理"})
    t = (await client.post(f"/teams{base}", headers=h, json={
        "name": "评审阵容", "role_keys": ["pm"]})).json()

    ok = await client.post(f"/workflows/import{base}", headers=h,
                           json={"spec": spec_with_agents("产品经理"),
                                 "loadout_id": t["id"]})
    assert ok.status_code == 200

    bad = await client.post(f"/workflows/import{base}", headers=h,
                            json={"spec": spec_with_agents("外部顾问"),
                                  "loadout_id": t["id"]})
    assert bad.status_code == 422
    codes = [i["code"] for i in bad.json()["detail"]["issues"]]
    assert "agent_not_in_loadout" in codes

    # dry_run 同样受锁定约束，且不落库
    dry = await client.post(f"/workflows/import{base}", headers=h,
                            json={"spec": spec_with_agents("外部顾问"),
                                  "loadout_id": t["id"], "dry_run": True})
    assert dry.status_code == 422
