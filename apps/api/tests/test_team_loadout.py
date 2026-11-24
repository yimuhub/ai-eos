"""阵容锁定（M3-B）：角色档案、从执行存阵容、spec 导入白名单。"""
import re

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.task import AgentExecution, Task
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
        for i, key in enumerate(("start", "p", "d", "end")):
            db.add(WorkflowStep(execution_id=ex.id, node_key=key,
                                node_type=NodeType.AGENT if key in ("p", "d")
                                else NodeType.START,
                                status=RunStatus.COMPLETED, attempt=1,
                                started_at=f"2026-11-20T10:00:0{i}+00:00",
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


def _snapshot_agent(key: str, agent_id: str) -> dict:
    return {"node_key": key, "node_type": "AGENT", "name": key,
            "config": {"agent_id": agent_id}}


@pytest.mark.asyncio
async def test_from_execution_uses_visited_snapshot_steps_and_historical_agent_execution(
        client, tenants):
    alice = tenants.alice
    alice_co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as db:
        actual = Agent(company_id=alice_co, name="实际员工")
        snapshot_only = Agent(company_id=alice_co, name="快照旧配置")
        current_only = Agent(company_id=alice_co, name="当前定义新增")
        unvisited = Agent(company_id=alice_co, name="未访问")
        failed = Agent(company_id=alice_co, name="失败节点")
        stale = Agent(company_id=alice_co, name="作废节点")
        foreign = Agent(company_id=tenants.bob_co["id"], name="外租户员工")
        db.add_all([actual, snapshot_only, current_only, unvisited, failed,
                    stale, foreign])
        await db.flush()
        wf = WorkflowDefinition(company_id=alice_co, name="执行证据",
                                created_by=alice["id"])
        db.add(wf)
        await db.flush()
        # 当前定义与 Task 的当前派工都可被改派；AgentExecution 才保留实际历史。
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="actual", node_type=NodeType.AGENT,
                         name="实际", config={"agent_id": current_only.id}),
            WorkflowNode(workflow_id=wf.id, node_key="current-only", node_type=NodeType.AGENT,
                         name="仅当前定义", config={"agent_id": current_only.id}),
        ])
        snapshot_nodes = [
            _snapshot_agent("actual", snapshot_only.id),
            _snapshot_agent("unvisited", unvisited.id),
            _snapshot_agent("failed", failed.id),
            _snapshot_agent("stale", stale.id),
            _snapshot_agent("foreign", foreign.id),
            {"node_key": "http", "node_type": "HTTP", "name": "非 AI",
             "config": {"agent_id": actual.id}},
        ]
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.COMPLETED,
                               started_by=alice["id"], current_nodes=["end"],
                               definition_snapshot={"nodes": snapshot_nodes, "edges": []})
        db.add(ex)
        await db.flush()
        tasks = [
            Task(company_id=alice_co, title="实际", assigned_agent_id=current_only.id,
                 created_by=alice["id"]),
            Task(company_id=alice_co, title="失败", assigned_agent_id=failed.id,
                 created_by=alice["id"]),
            Task(company_id=alice_co, title="作废", assigned_agent_id=stale.id,
                 created_by=alice["id"]),
            Task(company_id=alice_co, title="外租户", assigned_agent_id=foreign.id,
                 created_by=alice["id"]),
            Task(company_id=alice_co, title="HTTP", assigned_agent_id=actual.id,
                 created_by=alice["id"]),
        ]
        db.add_all(tasks)
        await db.flush()
        db.add_all([
            AgentExecution(agent_id=actual.id, task_id=tasks[0].id, status="completed"),
            AgentExecution(agent_id=failed.id, task_id=tasks[1].id, status="completed"),
            AgentExecution(agent_id=stale.id, task_id=tasks[2].id, status="completed"),
            # 任务当前派给外租户，历史执行仍属于外租户，必须排除。
            AgentExecution(agent_id=foreign.id, task_id=tasks[3].id, status="completed"),
        ])
        db.add_all([
            WorkflowStep(execution_id=ex.id, node_key="actual", node_type=NodeType.AGENT,
                         status=RunStatus.COMPLETED, attempt=1,
                         output_data={"task_id": tasks[0].id}),
            WorkflowStep(execution_id=ex.id, node_key="failed", node_type=NodeType.AGENT,
                         status=RunStatus.FAILED, attempt=1,
                         output_data={"task_id": tasks[1].id}),
            WorkflowStep(execution_id=ex.id, node_key="stale", node_type=NodeType.AGENT,
                         status=RunStatus.COMPLETED, attempt=1, stale_at="2026-11-20",
                         output_data={"task_id": tasks[2].id}),
            WorkflowStep(execution_id=ex.id, node_key="foreign", node_type=NodeType.AGENT,
                         status=RunStatus.COMPLETED, attempt=1,
                         output_data={"task_id": tasks[3].id}),
            WorkflowStep(execution_id=ex.id, node_key="http", node_type=NodeType.HTTP,
                         status=RunStatus.COMPLETED, attempt=1,
                         output_data={"task_id": tasks[4].id}),
        ])
        await db.commit()
        eid = ex.id
        excluded = [snapshot_only.id, current_only.id, unvisited.id, failed.id,
                    stale.id, foreign.id]

    r = await client.post("/teams/from-execution", headers=alice["headers"],
                          json={"execution_id": eid, "name": "实际阵容"})
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == [{"key": "role", "name": "实际员工"}]
    async with AsyncSessionLocal() as db:
        assert (await db.get(Agent, actual.id)).role_profile_id
        for aid in excluded:
            assert (await db.get(Agent, aid)).role_profile_id is None


@pytest.mark.asyncio
async def test_from_execution_without_completed_agents_returns_400(
        client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="没跑完")
        db.add(agent)
        await db.flush()
        wf = WorkflowDefinition(company_id=co["id"], name="无完成步骤",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        db.add(WorkflowNode(workflow_id=wf.id, node_key="agent", node_type=NodeType.AGENT,
                            name="员工", config={"agent_id": agent.id}))
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.FAILED,
                               started_by=user["id"], definition_snapshot={
                                   "nodes": [_snapshot_agent("agent", agent.id)],
                                   "edges": []})
        db.add(ex)
        await db.flush()
        db.add_all([
            WorkflowStep(execution_id=ex.id, node_key="agent", node_type=NodeType.AGENT,
                         status=RunStatus.FAILED, attempt=1),
            WorkflowStep(execution_id=ex.id, node_key="agent", node_type=NodeType.AGENT,
                         status=RunStatus.COMPLETED, attempt=2, stale_at="2026-11-20"),
        ])
        await db.commit()
        eid = ex.id
    r = await client.post("/teams/from-execution", headers=user["headers"],
                          json={"execution_id": eid, "name": "空阵容"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_from_execution_generates_collision_free_stable_ascii_keys(
        client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    async with AsyncSessionLocal() as db:
        db.add_all([
            RoleProfile(company_id=co["id"], key="role", name="已有角色"),
            RoleProfile(company_id=co["id"], key="data-analyst", name="已有分析"),
            RoleProfile(company_id=co["id"], key="中文历史-key", name="中文历史 key"),
        ])
        agents = [Agent(company_id=co["id"], name=name)
                  for name in ("张三", "李四", "Data Analyst")]
        db.add_all(agents)
        await db.flush()
        wf = WorkflowDefinition(company_id=co["id"], name="中文重名",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        for i, agent in enumerate(agents):
            db.add(WorkflowNode(workflow_id=wf.id, node_key=f"n{i}", node_type=NodeType.AGENT,
                                name=agent.name, config={"agent_id": agent.id}))
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.COMPLETED,
                               started_by=user["id"])
        db.add(ex)
        await db.flush()
        for i, agent in enumerate(agents):
            db.add(WorkflowStep(execution_id=ex.id, node_key=f"n{i}", node_type=NodeType.AGENT,
                                status=RunStatus.COMPLETED, attempt=1,
                                started_at=f"2026-11-20T10:00:0{i}+00:00"))
        await db.commit()
        eid = ex.id

    payload = {"execution_id": eid, "name": "碰撞测试"}
    r = await client.post("/teams/from-execution", headers=user["headers"], json=payload)
    assert r.status_code == 201, r.text
    roles = r.json()["roles"]
    assert {x["name"]: x["key"] for x in roles} == {
        "张三": "role-2", "李四": "role-3", "Data Analyst": "data-analyst-2",
    }
    assert all(re.match(r"^[a-z][a-z0-9_-]{0,63}$", x["key"]) for x in roles)
    assert len({x["key"] for x in roles}) == 3

    # 再次抽取复用已绑定档案，后缀不会因为调用顺序漂移。
    again = await client.post("/teams/from-execution", headers=user["headers"], json=payload)
    assert again.status_code == 201, again.text
    assert again.json()["roles"] == roles
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(RoleProfile).where(
            RoleProfile.company_id == co["id"]))).scalars().all()
        assert len(rows) == 6
        assert len({p.key for p in rows}) == 6
        assert "中文历史-key" in {p.key for p in rows}


@pytest.mark.asyncio
async def test_from_execution_revalidates_role_profile_company(
        client, make_user, make_company):
    user, co = await _company(client, make_user, make_company)
    other_user = await make_user()
    other_co = await make_company(other_user)
    async with AsyncSessionLocal() as db:
        foreign_profile = RoleProfile(company_id=other_co["id"], key="cross-tenant",
                                      name="外租户角色", source="custom")
        db.add(foreign_profile)
        await db.flush()
        agent = Agent(company_id=co["id"], name="本地员工",
                      role_profile_id=foreign_profile.id)
        db.add(agent)
        await db.flush()
        wf = WorkflowDefinition(company_id=co["id"], name="租户复核",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.COMPLETED,
                               started_by=user["id"], definition_snapshot={
                                   "nodes": [_snapshot_agent("agent", agent.id)],
                                   "edges": []})
        db.add(ex)
        await db.flush()
        db.add(WorkflowStep(execution_id=ex.id, node_key="agent", node_type=NodeType.AGENT,
                            status=RunStatus.COMPLETED, attempt=1))
        await db.commit()
        eid, foreign_id = ex.id, foreign_profile.id

    r = await client.post("/teams/from-execution", headers=user["headers"],
                          json={"execution_id": eid, "name": "本地阵容"})
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == [{"key": "role", "name": "本地员工"}]
    async with AsyncSessionLocal() as db:
        agent = await db.get(Agent, agent.id)
        profile = await db.get(RoleProfile, agent.role_profile_id)
        assert profile.company_id == co["id"]
        assert profile.key == "role" and profile.source == "from_execution"
        assert profile.id != foreign_id
        assert (await db.get(RoleProfile, foreign_id)).company_id == other_co["id"]


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
