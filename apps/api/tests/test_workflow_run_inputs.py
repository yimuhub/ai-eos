from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.project import Project
from app.models.workflow import WorkflowExecution
from app.spec import errors, validate
from tests.test_workflow_spec import make_spec


def _workflow(**over):
    body = {
        "name": "带入参的评审流",
        "inputs": [
            {"name": "subject", "type": "text", "required": True},
            {"name": "round", "type": "number", "default": 2},
            {"name": "approved", "type": "boolean", "default": False},
        ],
        "nodes": [
            {"node_key": "s", "node_type": "START", "name": "开始"},
            {"node_key": "a", "node_type": "AGENT", "name": "评审",
             "config": {"instruction": "评审 {{input.subject}}，第 {{input.round}} 轮"}},
            {"node_key": "e", "node_type": "END", "name": "结束"},
        ],
        "edges": [
            {"from_node": "s", "to_node": "a"},
            {"from_node": "a", "to_node": "e"},
        ],
    }
    body.update(over)
    return body


@pytest.mark.parametrize("policy", [
    {"max_nodes": "24"}, {"max_nodes": True}, {"max_nodes": 0},
    {"max_nodes": 25}, {"concurrency": 0}, {"concurrency": 9},
    {"concurrency": False}, {"budget_tokens": -1},
    {"budget_tokens": 2_000_001}, {"budget_tokens": 1.5},
])
def test_invalid_policy_is_reported_without_crashing(policy):
    issues = validate(make_spec(policy=policy))
    assert errors(issues)
    assert any(i.path.startswith("$.policy.") for i in errors(issues))


@pytest.mark.asyncio
async def test_run_inputs_are_declared_validated_and_persisted(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    async with AsyncSessionLocal() as db:
        from app.models.agent import Agent
        db.add(Project(id="project-a", company_id=co["id"], name="项目 A",
                       owner_id=alice["id"]))
        db.add(Agent(id="agent-a", company_id=co["id"], name="评审员"))
        body = _workflow(project_id="project-a")
        body["nodes"][1]["config"]["agent_id"] = "agent-a"
        await db.commit()
    saved = await client.post(f"/workflows?company_id={co['id']}",
                              json=body,
                              headers=alice["headers"])
    assert saved.status_code == 201, saved.text
    wid = saved.json()["id"]
    detail = await client.get(f"/workflows/{wid}", headers=alice["headers"])
    assert detail.status_code == 200
    assert [item["name"] for item in detail.json()["inputs"]] == [
        "subject", "round", "approved"]

    with patch("app.api.routes.workflows._run_in_background") as kick:
        missing = await client.post(f"/workflows/{wid}/run", json={},
                                    headers=alice["headers"])
        assert missing.status_code == 422
        assert kick.call_count == 0
        unknown = await client.post(f"/workflows/{wid}/run",
                                    json={"inputs": {"subject": "需求", "ghost": 1}},
                                    headers=alice["headers"])
        assert unknown.status_code == 422
        wrong = await client.post(f"/workflows/{wid}/run",
                                  json={"inputs": {"subject": "需求", "round": True}},
                                  headers=alice["headers"])
        assert wrong.status_code == 422
        foreign_project = await client.post(
            f"/workflows/{wid}/run",
            json={"inputs": {"subject": "需求"}, "project_id": "missing"},
            headers=alice["headers"])
        assert foreign_project.status_code == 404
        started = await client.post(
            f"/workflows/{wid}/run",
            json={"inputs": {"subject": "需求"}, "project_id": "project-a"},
            headers=alice["headers"])
        assert started.status_code == 202, started.text
        assert kick.call_count == 1
        assert started.json()["inputs"] == {
            "subject": "需求", "round": 2, "approved": False}
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, started.json()["execution_id"])
        assert ex.input_data == started.json()["inputs"]
        assert ex.project_id == "project-a"


@pytest.mark.asyncio
async def test_unresolved_input_reference_is_rejected_before_save(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    response = await client.post(
        f"/workflows?company_id={co['id']}",
        json=_workflow(inputs=[]), headers=alice["headers"])
    assert response.status_code == 422, response.text
    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(WorkflowExecution))
        assert count == 0


@pytest.mark.asyncio
async def test_sub_workflow_must_belong_to_same_company(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    bob = tenants.bob
    async with AsyncSessionLocal() as db:
        from app.models.workflow import WorkflowDefinition
        child = WorkflowDefinition(company_id=tenants.bob_co["id"], name="B 的子流程",
                                   created_by=bob["id"])
        db.add(child)
        await db.commit()
        child_id = child.id
    body = _workflow()
    body["nodes"][1] = {
        "node_key": "a", "node_type": "SUB_WORKFLOW", "name": "子流程",
        "config": {"workflow_id": child_id},
    }
    response = await client.post(f"/workflows?company_id={co['id']}",
                                 json=body, headers=alice["headers"])
    assert response.status_code in (400, 404), response.text
