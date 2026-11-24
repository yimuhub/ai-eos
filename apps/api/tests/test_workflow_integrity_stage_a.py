"""阶段 A 工作流完整性对抗测试。

全部使用 conftest 的临时数据库与进程内 ASGI 客户端；不启动后台 worker、
不调用模型、不访问真实工作流。测试故意直接改库模拟绕过 API 的脏数据，
验证运行时与快照边界仍然安全。
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.api.routes.workflows import _loadout_lock_issues
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.task import Task
from app.models.team import RoleProfile, TeamLoadout
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.workflow_runtime import (WorkflowRuntime, advance_execution,
                                  snapshot_workflow, start_workflow)


def _node(key: str, node_type: str, config: dict | None = None) -> dict:
    return {"node_key": key, "node_type": node_type, "name": key,
            "config": config or {}}


def _chain(*middle: dict) -> tuple[list[dict], list[dict]]:
    nodes = [_node("start", "START"), *middle, _node("end", "END")]
    keys = [n["node_key"] for n in nodes]
    edges = [{"from_node": a, "to_node": b} for a, b in zip(keys, keys[1:])]
    return nodes, edges


def _payload(nodes: list[dict], edges: list[dict], name: str = "完整性测试") -> dict:
    return {"name": name, "nodes": copy.deepcopy(nodes),
            "edges": copy.deepcopy(edges)}


async def _save(client, user, company_id: str, nodes: list[dict],
                edges: list[dict], name: str = "完整性测试") -> str:
    r = await client.post(f"/workflows?company_id={company_id}",
                          json=_payload(nodes, edges, name),
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _start(db, workflow_id: str, user_id: str, company_id: str):
    return await start_workflow(db, workflow_id, user_id,
                                input_data={"company_id": company_id})


async def _step(db, execution_id: str, node_key: str) -> WorkflowStep:
    row = (await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == execution_id,
        WorkflowStep.node_key == node_key))).scalars().one()
    return row


async def _dispatch_count(monkeypatch) -> list[str]:
    dispatched: list[str] = []
    monkeypatch.setattr("app.api.routes.workflows._run_in_background",
                        lambda execution_id: dispatched.append(execution_id))
    return dispatched


# ---------------------------------------------------------------------------
# 人工决定必须落在指定执行的指定步骤上
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_approval_executions_keep_decisions_isolated(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    nodes, edges = _chain(_node("approval", "APPROVAL"))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        first = await _start(db, wid, user["id"], co["id"])
        second = await _start(db, wid, user["id"], co["id"])
        first_id, second_id = first.id, second.id
        assert (await advance_execution(db, first_id)).status == RunStatus.WAITING_APPROVAL
        assert (await advance_execution(db, second_id)).status == RunStatus.WAITING_APPROVAL

    dispatched = await _dispatch_count(monkeypatch)
    r = await client.post(f"/executions/{first_id}/approve/approval",
                          params={"approve": True}, headers=user["headers"])
    assert r.status_code == 200, r.text

    async with AsyncSessionLocal() as db:
        first_ex = await db.get(WorkflowExecution, first_id)
        second_ex = await db.get(WorkflowExecution, second_id)
        assert first_ex.status == RunStatus.PENDING
        assert second_ex.status == RunStatus.WAITING_APPROVAL
        first_decision = (await _step(db, first_id, "approval")).input_data["_human_decision"]
        second_input = (await _step(db, second_id, "approval")).input_data
        assert first_decision["approved"] is True
        assert first_decision["decided_by"] == user["id"]
        assert "_human_decision" not in second_input

    async with AsyncSessionLocal() as db:
        assert (await advance_execution(db, first_id)).status == RunStatus.COMPLETED
        untouched = await db.get(WorkflowExecution, second_id)
        assert untouched.status == RunStatus.WAITING_APPROVAL
        assert untouched.current_nodes == ["approval"]

    r = await client.post(f"/executions/{second_id}/approve/approval",
                          params={"approve": False}, headers=user["headers"])
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        rejected = await db.get(WorkflowExecution, second_id)
        decision = (await _step(db, second_id, "approval")).input_data["_human_decision"]
        assert rejected.status == RunStatus.CANCELLED
        assert decision["approved"] is False
        still_first = (await _step(db, first_id, "approval")).input_data["_human_decision"]
        assert still_first["approved"] is True
    assert dispatched == [first_id], "拒绝审批不应再次投递执行"


@pytest.mark.asyncio
async def test_two_human_input_executions_keep_text_and_outputs_isolated(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    nodes, edges = _chain(_node("input", "HUMAN_INPUT"))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        first = await _start(db, wid, user["id"], co["id"])
        second = await _start(db, wid, user["id"], co["id"])
        first_id, second_id = first.id, second.id
        await advance_execution(db, first_id)
        await advance_execution(db, second_id)
        assert first.status == second.status == RunStatus.WAITING_INPUT

    dispatched = await _dispatch_count(monkeypatch)
    for eid, text in ((first_id, "第一执行的专属输入"), (second_id, "第二执行的专属输入")):
        r = await client.post(f"/executions/{eid}/approve/input",
                              params={"human_input": text}, headers=user["headers"])
        assert r.status_code == 200, r.text

    async with AsyncSessionLocal() as db:
        first_text = (await _step(db, first_id, "input")).input_data["_human_decision"]["human_input"]
        second_text = (await _step(db, second_id, "input")).input_data["_human_decision"]["human_input"]
        assert first_text == "第一执行的专属输入"
        assert second_text == "第二执行的专属输入"
        assert (await advance_execution(db, first_id)).status == RunStatus.COMPLETED
        assert (await advance_execution(db, second_id)).status == RunStatus.COMPLETED
        first_out = (await _step(db, first_id, "input")).output_data
        second_out = (await _step(db, second_id, "input")).output_data
        assert first_out == {"human_input": "第一执行的专属输入"}
        assert second_out == {"human_input": "第二执行的专属输入"}
    assert dispatched == [first_id, second_id]


@pytest.mark.asyncio
async def test_other_tenant_cannot_decide_waiting_approval(client, tenants, monkeypatch):
    nodes, edges = _chain(_node("approval", "APPROVAL"))
    wid = await _save(client, tenants.alice, tenants.alice_co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, tenants.alice["id"], tenants.alice_co["id"])
        eid = ex.id
        await advance_execution(db, eid)
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/approve/approval",
                          params={"approve": True}, headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        decision = (await _step(db, eid, "approval")).input_data
        assert ex.status == RunStatus.WAITING_APPROVAL
        assert "_human_decision" not in decision
    assert dispatched == []


@pytest.mark.asyncio
async def test_approval_rejects_non_current_node(client, make_user, make_company,
                                                monkeypatch):
    user = await make_user()
    co = await make_company(user)
    nodes = [_node("start", "START"), _node("first", "APPROVAL"),
             _node("second", "APPROVAL"), _node("end", "END")]
    edges = [{"from_node": a, "to_node": b} for a, b in
             (("start", "first"), ("first", "second"), ("second", "end"))]
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        await advance_execution(db, eid)
        assert ex.current_nodes == ["first"]
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/approve/second",
                          params={"approve": True}, headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        assert ex.status == RunStatus.WAITING_APPROVAL
        assert ex.current_nodes == ["first"]
        assert "_human_decision" not in (await _step(db, eid, "first")).input_data
        assert (await db.execute(select(WorkflowStep.id).where(
            WorkflowStep.execution_id == eid,
            WorkflowStep.node_key == "second"))).first() is None
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type,status", [
    ("APPROVAL", RunStatus.RUNNING),
    ("APPROVAL", RunStatus.PENDING),
    ("APPROVAL", RunStatus.FAILED),
    ("APPROVAL", RunStatus.WAITING_INPUT),
    ("HUMAN_INPUT", RunStatus.WAITING_APPROVAL),
    ("HUMAN_INPUT", RunStatus.COMPLETED),
])
async def test_approval_rejects_wrong_execution_state(
        client, make_user, make_company, monkeypatch, node_type, status):
    user = await make_user()
    co = await make_company(user)
    nodes, edges = _chain(_node("human", node_type))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        await advance_execution(db, eid)
        ex.status = status
        ex.current_nodes = ["human"]
        await db.commit()
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/approve/human",
                          params={"approve": True, "human_input": "不应写入"},
                          headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        decision = (await _step(db, eid, "human")).input_data
        assert ex.status == status
        assert "_human_decision" not in decision
    assert dispatched == []


@pytest.mark.asyncio
async def test_approval_rejects_agent_node_and_missing_human_input(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="人工节点前置员工")
        db.add(agent)
        await db.commit()
        agent_id = agent.id
    nodes, edges = _chain(_node("agent", "AGENT", {"agent_id": agent_id}),
                          _node("input", "HUMAN_INPUT"))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        ex.current_nodes = ["agent", "input"]
        ex.status = RunStatus.WAITING_INPUT
        db.add(WorkflowStep(execution_id=eid, node_key="input",
                            node_type=NodeType.HUMAN_INPUT, status=RunStatus.RUNNING,
                            attempt=1, input_data={}))
        await db.commit()
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/approve/agent",
                          params={"approve": True}, headers=user["headers"])
    assert r.status_code == 400, r.text
    r = await client.post(f"/executions/{eid}/approve/input",
                          params={"approve": True}, headers=user["headers"])
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        decision = (await _step(db, eid, "input")).input_data
        assert ex.status == RunStatus.WAITING_INPUT
        assert "_human_decision" not in decision
    assert dispatched == []


@pytest.mark.asyncio
async def test_approval_rejects_waiting_node_without_open_step(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    nodes, edges = _chain(_node("approval", "APPROVAL"))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        ex.status = RunStatus.WAITING_APPROVAL
        ex.current_nodes = ["approval"]
        await db.commit()
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/approve/approval",
                          params={"approve": True}, headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        assert ex.status == RunStatus.WAITING_APPROVAL
        assert ex.current_nodes == ["approval"]
        assert (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid))).first() is None
    assert dispatched == []


# ---------------------------------------------------------------------------
# 员工身份、租户与阵容锁定
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "", "missing-agent"])
async def test_save_and_update_reject_empty_or_unknown_agent_id(
        client, make_user, make_company, agent_id):
    user = await make_user()
    co = await make_company(user)
    clean_nodes, clean_edges = _chain(_node("task", "TASK"))
    wid = await _save(client, user, co["id"], clean_nodes, clean_edges)
    bad_nodes, bad_edges = _chain(_node("agent", "AGENT", {"agent_id": agent_id}))

    r = await client.post(f"/workflows?company_id={co['id']}",
                          json=_payload(bad_nodes, bad_edges), headers=user["headers"])
    assert r.status_code == 400, r.text
    r = await client.put(f"/workflows/{wid}", json=_payload(bad_nodes, bad_edges),
                         headers=user["headers"])
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        assert len((await db.execute(select(WorkflowDefinition))).scalars().all()) == 1
        wf = await db.get(WorkflowDefinition, wid)
        rows = (await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == wid))).scalars().all()
        assert wf.version == 1
        assert {n.node_key for n in rows} == {"start", "task", "end"}


@pytest.mark.asyncio
async def test_empty_company_rejects_foreign_agent_id_and_name_on_import(
        client, tenants):
    async with AsyncSessionLocal() as db:
        foreign = Agent(company_id=tenants.bob_co["id"], name="外来员工")
        db.add(foreign)
        await db.commit()
        foreign_id, foreign_name = foreign.id, foreign.name

    base = {
        "spec": "v1", "key": "foreign-agent", "name": "外来员工测试",
        "nodes": [
            {"key": "start", "type": "START"},
            {"key": "agent", "type": "AGENT", "task": "执行任务"},
            {"key": "end", "type": "END"},
        ],
        "edges": [{"from": "start", "to": "agent"},
                  {"from": "agent", "to": "end"}],
    }
    for identity in ({"agent_id": foreign_id}, {"agent": foreign_name}):
        spec = copy.deepcopy(base)
        spec["nodes"][1].update(identity)
        r = await client.post(f"/workflows/import?company_id={tenants.alice_co['id']}",
                              json={"spec": spec}, headers=tenants.alice["headers"])
        assert r.status_code == 422, r.text
        codes = {i["code"] for i in r.json()["detail"]["issues"]}
        assert "agent_not_found" in codes
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(WorkflowDefinition.id).where(
            WorkflowDefinition.company_id == tenants.alice_co["id"]))).first() is None

    nodes, edges = _chain(_node("agent", "AGENT", {"agent_id": foreign_id}))
    r = await client.post(f"/workflows?company_id={tenants.alice_co['id']}",
                          json=_payload(nodes, edges), headers=tenants.alice["headers"])
    assert r.status_code == 400, r.text
    clean_nodes, clean_edges = _chain(_node("task", "TASK"))
    wid = await _save(client, tenants.alice, tenants.alice_co["id"], clean_nodes, clean_edges)
    r = await client.put(f"/workflows/{wid}", json=_payload(nodes, edges),
                         headers=tenants.alice["headers"])
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_import_rejects_agent_name_id_conflict(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        first = Agent(company_id=co["id"], name="真正员工")
        second = Agent(company_id=co["id"], name="另一员工")
        db.add_all([first, second])
        await db.commit()
        first_id, second_name = first.id, second.name
    spec = {
        "spec": "v1", "key": "conflicting-agent", "name": "身份冲突测试",
        "nodes": [
            {"key": "start", "type": "START"},
            {"key": "agent", "type": "AGENT", "agent_id": first_id,
             "agent": second_name, "task": "执行任务"},
            {"key": "end", "type": "END"},
        ],
        "edges": [{"from": "start", "to": "agent"},
                  {"from": "agent", "to": "end"}],
    }
    r = await client.post(f"/workflows/import?company_id={co['id']}",
                          json={"spec": spec}, headers=user["headers"])
    assert r.status_code == 422, r.text
    issues = r.json()["detail"]["issues"]
    assert any(i["code"] == "agent_identity_mismatch" and i["path"] == "nodes[1].agent"
               for i in issues)
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(WorkflowDefinition.id))).first() is None


@pytest.mark.asyncio
async def test_loadout_lock_uses_final_resolved_employee_and_rejects_conflicts(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        allowed = Agent(company_id=co["id"], name="最终解析员")
        outsider = Agent(company_id=co["id"], name="阵容外员工")
        db.add_all([allowed, outsider])
        await db.flush()
        profile = RoleProfile(company_id=co["id"], key="resolved-role",
                              name="最终解析员")
        loadout = TeamLoadout(company_id=co["id"], name="锁定阵容",
                              roles=[{"key": profile.key, "name": profile.name}],
                              created_by=user["id"])
        db.add_all([profile, loadout])
        await db.commit()
        allowed_id, outsider_id = allowed.id, outsider.id
        loadout_id = loadout.id

    async with AsyncSessionLocal() as db:
        def spec(identity: dict) -> dict:
            return {"nodes": [{"key": "agent", "type": "AGENT", **identity}]}

        assert await _loadout_lock_issues(db, co["id"], loadout_id,
                                          spec({"agent_id": allowed_id})) == []
        assert await _loadout_lock_issues(db, co["id"], loadout_id,
                                          spec({"agent": " 最 终 解 析 员 "})) == []
        # agent_id 优先于 name：最终解析到允许员工，不能因 name 本身不在白名单误杀。
        assert await _loadout_lock_issues(db, co["id"], loadout_id,
                                          spec({"agent_id": allowed_id,
                                                "agent": "最终解析员"})) == []
        # 无效 id 最终按有效 name 解析；普通 validate 会另外拒绝无效 id。
        assert await _loadout_lock_issues(db, co["id"], loadout_id,
                                          spec({"agent_id": "missing",
                                                "agent": "最终解析员"})) == []

        issues = await _loadout_lock_issues(db, co["id"], loadout_id,
                                            spec({"agent_id": outsider_id}))
        assert [i.code for i in issues] == ["agent_not_in_loadout"]
        assert issues[0].path == "nodes[0].agent"
        # name 指向允许员工也不能覆盖优先解析到的阵容外 agent_id。
        issues = await _loadout_lock_issues(db, co["id"], loadout_id,
                                            spec({"agent_id": outsider_id,
                                                  "agent": "最终解析员"}))
        assert [i.code for i in issues] == ["agent_identity_mismatch"]
        issues = await _loadout_lock_issues(db, co["id"], loadout_id,
                                            spec({"agent_id": allowed_id,
                                                  "agent": "阵容外员工"}))
        assert [i.code for i in issues] == ["agent_identity_mismatch"]


@pytest.mark.asyncio
async def test_runtime_rejects_cross_company_agent_before_creating_task(
        client, tenants):
    async with AsyncSessionLocal() as db:
        foreign = Agent(company_id=tenants.bob_co["id"], name="B 公司员工")
        db.add(foreign)
        await db.flush()
        wf = WorkflowDefinition(company_id=tenants.alice_co["id"],
                                name="跨公司员工运行测试", created_by=tenants.alice["id"])
        db.add(wf)
        await db.flush()
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="start", node_type=NodeType.START,
                         name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="agent", node_type=NodeType.AGENT,
                         name="执行", config={"agent_id": foreign.id}),
            WorkflowNode(workflow_id=wf.id, node_key="end", node_type=NodeType.END,
                         name="结束"),
        ])
        db.add_all([
            WorkflowEdge(workflow_id=wf.id, from_node="start", to_node="agent"),
            WorkflowEdge(workflow_id=wf.id, from_node="agent", to_node="end"),
        ])
        await db.commit()
        ex = await _start(db, wf.id, tenants.alice["id"], tenants.alice_co["id"])
        eid, foreign_id = ex.id, foreign.id
        result = await advance_execution(db, eid)

    assert result.status == RunStatus.FAILED
    assert "does not belong to the workflow company" in result.error
    async with AsyncSessionLocal() as db:
        steps = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid))).scalars().all()
        assert {s.node_key for s in steps} == {"start", "agent"}
        assert next(s for s in steps if s.node_key == "agent").status == RunStatus.FAILED
        assert (await db.execute(select(Task.id).where(
            Task.assigned_agent_id == foreign_id))).first() is None


@pytest.mark.asyncio
async def test_runtime_ignores_forged_input_company_id(client, tenants, monkeypatch):
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=tenants.alice_co["id"], name="本公司员工")
        db.add(agent)
        await db.flush()
        wf = WorkflowDefinition(company_id=tenants.alice_co["id"],
                                name="入参租户伪造测试", created_by=tenants.alice["id"])
        db.add(wf)
        await db.flush()
        node = WorkflowNode(workflow_id=wf.id, node_key="agent",
                            node_type=NodeType.AGENT, name="执行",
                            config={"agent_id": agent.id})
        db.add(node)
        ex = WorkflowExecution(
            workflow_id=wf.id, status=RunStatus.RUNNING,
            started_by=tenants.alice["id"],
            input_data={"company_id": tenants.bob_co["id"]})
        db.add(ex)
        await db.commit()
        execution_id, workflow_id, node_id = ex.id, wf.id, node.id

    async def fake_run(db, agent, task, **kwargs):
        task.result = "完成"

    async def fake_transition(db, task, status):
        task.status = status

    monkeypatch.setattr("app.agent_runtime.run_agent_on_task", fake_run)
    monkeypatch.setattr("app.workflow_runtime.transition_task", fake_transition)
    async with AsyncSessionLocal() as db:
        runtime = WorkflowRuntime(db, await db.get(WorkflowExecution, execution_id))
        current_node = await db.get(WorkflowNode, node_id)
        assert await runtime._resolve_company() == tenants.alice_co["id"]
        result = await runtime._exec_agent_node(current_node, {})
        task = await db.get(Task, result["task_id"])
        assert task.company_id == tenants.alice_co["id"]
        assert task.company_id != runtime.exec_input_company()
        assert await db.get(WorkflowDefinition, workflow_id) is not None
        await db.commit()


# ---------------------------------------------------------------------------
# 返工只允许已成功执行的 AGENT，且不能抢等待人工的执行
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rework_unexecuted_agent_returns_409(client, make_user, make_company,
                                                  monkeypatch):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="尚未执行")
        db.add(agent)
        await db.commit()
        agent_id = agent.id
    nodes, edges = _chain(_node("agent", "AGENT", {"agent_id": agent_id}))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/rework",
                          json={"node_key": "agent", "mode": "rerun"},
                          headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        assert ex.status == RunStatus.PENDING
        assert ex.current_nodes == ["start"]
        assert (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid))).first() is None
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("node_key", ["start", "end", "approval", "input"])
async def test_rework_non_agent_returns_400(client, make_user, make_company,
                                           monkeypatch, node_key):
    user = await make_user()
    co = await make_company(user)
    nodes = [_node("start", "START"), _node("approval", "APPROVAL"),
             _node("input", "HUMAN_INPUT"), _node("end", "END")]
    keys = [n["node_key"] for n in nodes]
    edges = [{"from_node": a, "to_node": b} for a, b in zip(keys, keys[1:])]
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        ex.status = RunStatus.COMPLETED
        await db.commit()
    dispatched = await _dispatch_count(monkeypatch)

    r = await client.post(f"/executions/{eid}/rework",
                          json={"node_key": node_key, "mode": "rerun"},
                          headers=user["headers"])
    assert r.status_code == 400, r.text
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("human_type,waiting", [
    ("APPROVAL", RunStatus.WAITING_APPROVAL),
    ("HUMAN_INPUT", RunStatus.WAITING_INPUT),
])
@pytest.mark.parametrize("target", [None, "agent"])
async def test_rework_waiting_human_execution_returns_409(
        client, make_user, make_company, monkeypatch, human_type, waiting, target):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="返工员工")
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    async def fake_agent(self, node, ctx, gate_feedback=None):
        return {"output": "已完成"}

    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_agent)
    nodes, edges = _chain(_node("agent", "AGENT", {"agent_id": agent_id}),
                          _node("human", human_type))
    wid = await _save(client, user, co["id"], nodes, edges)
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        result = await advance_execution(db, eid)
        assert result.status == waiting
        completed = await _step(db, eid, "agent")
        assert completed.status == RunStatus.COMPLETED
    dispatched = await _dispatch_count(monkeypatch)

    body = {"mode": "rerun"}
    if target is not None:
        body["node_key"] = target
    r = await client.post(f"/executions/{eid}/rework", json=body,
                          headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, eid)
        step = await _step(db, eid, "agent")
        assert ex.status == waiting
        assert ex.current_nodes == ["human"]
        assert step.status == RunStatus.COMPLETED and step.stale_at is None
    assert dispatched == []


# ---------------------------------------------------------------------------
# 保存/更新图完整性与活跃执行锁
# ---------------------------------------------------------------------------

def _invalid_graph(case: str) -> tuple[list[dict], list[dict]]:
    nodes, edges = _chain(_node("agent", "TASK"))
    if case == "dangling":
        edges.append({"from_node": "agent", "to_node": "ghost"})
    elif case == "duplicate":
        edges.append(copy.deepcopy(edges[0]))
    elif case == "self_loop":
        edges.append({"from_node": "agent", "to_node": "agent"})
    elif case == "cycle":
        edges.append({"from_node": "end", "to_node": "agent"})
    elif case == "disconnected":
        # 移除唯一通往 END 的边：START 与 END 之间断开，agent 成为断头路。
        edges = edges[:1]
    else:  # pragma: no cover - 参数由测试固定
        raise AssertionError(case)
    return nodes, edges


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["dangling", "duplicate", "self_loop", "cycle",
                                  "disconnected"])
async def test_save_and_update_reject_invalid_graph_without_partial_write(
        client, make_user, make_company, case):
    user = await make_user()
    co = await make_company(user)
    clean_nodes, clean_edges = _chain(_node("task", "TASK"))
    wid = await _save(client, user, co["id"], clean_nodes, clean_edges, name="原定义")
    bad_nodes, bad_edges = _invalid_graph(case)

    r = await client.post(f"/workflows?company_id={co['id']}",
                          json=_payload(bad_nodes, bad_edges, "非法新建"),
                          headers=user["headers"])
    assert r.status_code == 400, r.text
    r = await client.put(f"/workflows/{wid}",
                         json=_payload(bad_nodes, bad_edges, "非法修改"),
                         headers=user["headers"])
    assert r.status_code == 400, r.text

    async with AsyncSessionLocal() as db:
        definitions = (await db.execute(select(WorkflowDefinition))).scalars().all()
        assert len(definitions) == 1
        wf = await db.get(WorkflowDefinition, wid)
        assert wf.name == "原定义" and wf.version == 1
        nodes = (await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == wid))).scalars().all()
        edges = (await db.execute(select(WorkflowEdge).where(
            WorkflowEdge.workflow_id == wid))).scalars().all()
        assert {n.node_key for n in nodes} == {"start", "task", "end"}
        assert {(e.from_node, e.to_node) for e in edges} == {
            ("start", "task"), ("task", "end")}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING,
                                    RunStatus.PAUSED, RunStatus.WAITING_APPROVAL,
                                    RunStatus.WAITING_INPUT])
async def test_active_execution_blocks_definition_update(
        client, make_user, make_company, status):
    user = await make_user()
    co = await make_company(user)
    nodes, edges = _chain(_node("task", "TASK"))
    wid = await _save(client, user, co["id"], nodes, edges, name="锁定定义")
    async with AsyncSessionLocal() as db:
        ex = await _start(db, wid, user["id"], co["id"])
        ex.status = status
        await db.commit()

    new_nodes = [_node("start", "START"), _node("replacement", "TASK"),
                 _node("end", "END")]
    new_edges = [{"from_node": "start", "to_node": "replacement"},
                 {"from_node": "replacement", "to_node": "end"}]
    r = await client.put(f"/workflows/{wid}",
                         json=_payload(new_nodes, new_edges, "不应生效"),
                         headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        wf = await db.get(WorkflowDefinition, wid)
        rows = (await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == wid))).scalars().all()
        saved_edges = (await db.execute(select(WorkflowEdge).where(
            WorkflowEdge.workflow_id == wid))).scalars().all()
        assert wf.name == "锁定定义" and wf.version == 1
        assert {n.node_key for n in rows} == {"start", "task", "end"}
        assert {(e.from_node, e.to_node) for e in saved_edges} == {
            ("start", "task"), ("task", "end")}


# ---------------------------------------------------------------------------
# 执行定义快照：后续改图不能偷换正在执行的工作流
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_start_and_runtime_graph_survive_later_definition_changes(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="快照员工")
        db.add(agent)
        await db.flush()
        wf = WorkflowDefinition(company_id=co["id"], name="快照原定义",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="start", node_type=NodeType.START,
                         name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="old_agent", node_type=NodeType.AGENT,
                         name="旧员工节点", config={"agent_id": agent.id,
                                                     "instruction": "旧指令"}),
            WorkflowNode(workflow_id=wf.id, node_key="end", node_type=NodeType.END,
                         name="结束"),
        ])
        db.add_all([
            WorkflowEdge(workflow_id=wf.id, from_node="start", to_node="old_agent"),
            WorkflowEdge(workflow_id=wf.id, from_node="old_agent", to_node="end"),
        ])
        await db.commit()
        wid = wf.id
        frozen = await snapshot_workflow(db, wid)
        original = copy.deepcopy(frozen)
        ex = await _start(db, wid, user["id"], co["id"])
        eid = ex.id
        assert ex.definition_snapshot == original

        # 模拟定义被后续编辑：节点、员工配置与边全部替换。
        await db.execute(delete(WorkflowEdge).where(WorkflowEdge.workflow_id == wid))
        await db.execute(delete(WorkflowNode).where(WorkflowNode.workflow_id == wid))
        db.add_all([
            WorkflowNode(workflow_id=wid, node_key="start", node_type=NodeType.START,
                         name="新开始"),
            WorkflowNode(workflow_id=wid, node_key="new_task", node_type=NodeType.TASK,
                         name="新任务", config={"replacement": True}),
            WorkflowNode(workflow_id=wid, node_key="end", node_type=NodeType.END,
                         name="新结束"),
        ])
        db.add_all([
            WorkflowEdge(workflow_id=wid, from_node="start", to_node="new_task"),
            WorkflowEdge(workflow_id=wid, from_node="new_task", to_node="end"),
        ])
        await db.commit()
        current = await snapshot_workflow(db, wid)
        assert {n["node_key"] for n in current["nodes"]} == {"start", "new_task", "end"}
        assert frozen == original, "snapshot_workflow 返回了可变引用"

        runtime = WorkflowRuntime(db, ex)
        graph, out_edges = await runtime._load_graph()
        assert set(graph) == {"start", "old_agent", "end"}
        assert graph["old_agent"].name == "旧员工节点"
        assert graph["old_agent"].config["instruction"] == "旧指令"
        assert [e.to_node for e in out_edges["start"]] == ["old_agent"]
        graph_again, edges_again = await runtime._load_graph()
        assert {k: (n.name, n.config) for k, n in graph_again.items()} == {
            k: (n.name, n.config) for k, n in graph.items()}
        assert {k: [(e.from_node, e.to_node, e.label) for e in es]
                for k, es in edges_again.items()} == {
            k: [(e.from_node, e.to_node, e.label) for e in es]
            for k, es in out_edges.items()}

    seen: list[tuple[str, str]] = []

    async def fake_agent(self, node, ctx, gate_feedback=None):
        seen.append((node.node_key, node.config["instruction"]))
        return {"output": "旧图执行完成"}

    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_agent)
    async with AsyncSessionLocal() as db:
        result = await advance_execution(db, eid)
        assert result.status == RunStatus.COMPLETED, result.error
        assert result.definition_snapshot == original
    assert seen == [("old_agent", "旧指令")]
    async with AsyncSessionLocal() as db:
        steps = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid))).scalars().all()
        assert {s.node_key for s in steps} == {"start", "old_agent", "end"}
