"""CEO 工作流生成与自优化：图校验、评分、版本升级、反馈接口。

全部离线：外呼开关在 conftest 里关闭，本文件按用例显式打开并 monkeypatch
模型返回；不访问知识库以外的东西，也不真正跑工作流节点。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import ceo_engine
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.ceo import CeoMission
from app.models.event import EventLog
from app.models.workflow import (NodeType, WorkflowDefinition, WorkflowEdge,
                                 WorkflowFeedback, WorkflowNode, WorkflowStatus)
from app.workflow_authoring import (apply_mission_feedback,
                                    author_mission_workflow,
                                    deterministic_graph, score_mission)

pytestmark = pytest.mark.asyncio


async def _agent(client, user, company_id: str, name: str, role: str = "专家") -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": role},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _setup(client, make_user, make_company, names=("王总", "小李")):
    user = await make_user()
    co = await make_company(user)
    ids = {}
    for i, name in enumerate(names):
        ids[name] = await _agent(client, user, co["id"], name,
                                 "总经理" if i == 0 else "专家")
    return user, co, ids


def _mission(db, user, co, ids, title: str, subtasks: list[dict]) -> CeoMission:
    mission = CeoMission(
        company_id=co["id"], created_by=user["id"], title=title,
        instruction="把 Q3 线索量提升 30%", status="RUNNING",
        objective="把 Q3 线索量提升 30%", deliverable="增长方案 growth.md",
        redlines=["不得编造数据"], acceptance=["每个结论有出处"],
        terminology={}, extra_instructions=[], anchor_version=1,
        coordinator_agent_id=ids["王总"], max_parallel=3,
        plan=subtasks, subtask_count=len(subtasks), new_agents=[])
    db.add(mission)
    return mission


def _enable(monkeypatch, llm: bool = False):
    monkeypatch.setenv("AIEOS_CEO_WORKFLOW_AUTHOR", "1")
    monkeypatch.setenv("AIEOS_CEO_WORKFLOW_LLM", "1" if llm else "0")
    monkeypatch.setenv("AIEOS_CEO_WEB_RESEARCH", "0")


def _subtasks(ids, n: int = 2) -> list[dict]:
    names = ["王总", "小李"]
    return [{"index": i, "title": f"子任务{i + 1}",
             "description": f"负责第 {i + 1} 部分，边界写清楚",
             "agent_id": ids[names[i % len(names)]],
             "agent_name": names[i % len(names)],
             "agent_matched": True, "new_agent_index": None,
             "required_skills": ["资料检索"]}
            for i in range(n)]


async def _nodes(db, workflow_id: str) -> list[WorkflowNode]:
    return (await db.execute(
        select(WorkflowNode).where(WorkflowNode.workflow_id == workflow_id)
        .order_by(WorkflowNode.position_x))).scalars().all()


async def test_authoring_builds_deterministic_graph_and_audit_event(
        client, make_user, make_company, monkeypatch):
    _enable(monkeypatch)
    user, co, ids = await _setup(client, make_user, make_company)
    async with AsyncSessionLocal() as s:
        mission = _mission(s, user, co, ids, "季度增长方案",
                           _subtasks(ids, 2))
        s.add(mission)
        await s.flush()
        wf = await author_mission_workflow(s, mission, await s.get(
            Agent, ids["王总"]))
        await s.commit()
        await s.refresh(wf)
        assert wf is not None
        assert mission.workflow_id == wf.id
        assert wf.version == 1 and wf.source == "ceo"
        assert wf.reuse_key == "季度增长方案"
        assert wf.status == WorkflowStatus.PUBLISHED
        nodes = await _nodes(s, wf.id)
        assert [n.node_type for n in nodes] == [
            NodeType.START, NodeType.AGENT, NodeType.AGENT, NodeType.END]
        assert all(n.config.get("agent_id") for n in nodes
                   if n.node_type == NodeType.AGENT)
        assert "必备技能：资料检索" in nodes[1].config["instruction"]
        edges = (await s.execute(select(WorkflowEdge).where(
            WorkflowEdge.workflow_id == wf.id))).scalars().all()
        assert len(edges) == 3
        event = (await s.execute(select(EventLog).where(
            EventLog.event_type == "workflow.generated",
            EventLog.entity_id == wf.id))).scalars().first()
        assert event is not None
        assert event.payload["version"] == 1


async def test_score_and_optimize_creates_new_version_with_lessons(
        client, make_user, make_company, monkeypatch):
    _enable(monkeypatch)
    user, co, ids = await _setup(client, make_user, make_company)
    results = [
        {"title": "子任务1", "status": "COMPLETED", "agent_name": "王总"},
        {"title": "子任务2", "status": "FAILED", "agent_name": "小李",
         "error": "模型超时"},
    ]
    async with AsyncSessionLocal() as s:
        mission = _mission(s, user, co, ids, "季度增长方案", _subtasks(ids, 2))
        s.add(mission)
        await s.flush()
        agent = await s.get(Agent, ids["王总"])
        first = await author_mission_workflow(s, mission, agent)
        await s.commit()
        first_id = first.id

        mission.status = "COMPLETED"
        mission.completed_count = 1
        mission.failed_count = 1
        mission.off_track_count = 0
        mission.retry_count = 0
        mission.stats = score_mission(mission, results)["metrics"]
        summary = await apply_mission_feedback(s, mission, results)
        await s.commit()

        assert summary is not None
        assert summary["workflow_score"] == 25.0, summary
        new = await s.get(WorkflowDefinition, mission.workflow_id)
        old = await s.get(WorkflowDefinition, first_id)
        assert new is not None and new.id != first_id
        assert new.version == 2
        assert new.previous_workflow_id == first_id
        assert old.status == WorkflowStatus.ARCHIVED
        instructions = [n.config.get("instruction", "")
                        for n in await _nodes(s, new.id)
                        if n.node_type == NodeType.AGENT]
        assert any("模型超时" in text for text in instructions)
        feedback = (await s.execute(select(WorkflowFeedback).where(
            WorkflowFeedback.workflow_id == first_id))).scalars().all()
        assert len(feedback) == 1 and feedback[0].score == 25.0
        assert feedback[0].success is False
        optimized = (await s.execute(select(EventLog).where(
            EventLog.event_type == "workflow.optimized",
            EventLog.entity_id == new.id))).scalars().first()
        assert optimized is not None


async def test_llm_graph_is_validated_and_bad_json_falls_back(
        client, make_user, make_company, monkeypatch):
    _enable(monkeypatch, llm=True)

    async def _fake(db, agent, messages, max_tokens=1600):
        return json.dumps({
            "nodes": [
                {"key": "start", "type": "START", "name": "开始"},
                {"key": "step", "type": "AGENT", "name": "写方案",
                 "agent": "王总", "instruction": "按准绳写一版方案"},
                {"key": "check", "type": "APPROVAL", "name": "老板复核"},
                {"key": "end", "type": "END", "name": "结束"},
            ],
            "edges": [{"from": "start", "to": "step"},
                      {"from": "step", "to": "check"},
                      {"from": "check", "to": "end"}],
        }, ensure_ascii=False)

    monkeypatch.setattr(ceo_engine, "_chat", _fake)
    user, co, ids = await _setup(client, make_user, make_company)
    async with AsyncSessionLocal() as s:
        mission = _mission(s, user, co, ids, "季度增长方案", _subtasks(ids, 2))
        s.add(mission)
        await s.flush()
        agent = await s.get(Agent, ids["王总"])
        wf = await author_mission_workflow(s, mission, agent)
        await s.commit()
        nodes = await _nodes(s, wf.id)
        assert [n.node_type for n in nodes] == [
            NodeType.START, NodeType.AGENT, NodeType.APPROVAL, NodeType.END]
        assert nodes[1].config["agent_id"] == ids["王总"]
        assert nodes[1].config["instruction"] == "按准绳写一版方案"

        # 模型返回坏 JSON：退到确定性图，并留下可审计原因
        async def _broken(db, agent, messages, max_tokens=1600):
            return "这不是 JSON"

        monkeypatch.setattr(ceo_engine, "_chat", _broken)
        mission2 = _mission(s, user, co, ids, "渠道复盘", _subtasks(ids, 2))
        s.add(mission2)
        await s.flush()
        wf2 = await author_mission_workflow(s, mission2, agent)
        await s.commit()
        nodes2 = await _nodes(s, wf2.id)
        assert [n.node_type for n in nodes2] == [
            NodeType.START, NodeType.AGENT, NodeType.AGENT, NodeType.END]
        assert any("模型图不可用" in e for e in (wf2.meta or {})["errors"])


async def test_same_title_and_plan_reuses_healthy_workflow(
        client, make_user, make_company, monkeypatch):
    _enable(monkeypatch)
    user, co, ids = await _setup(client, make_user, make_company)
    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, ids["王总"])
        m1 = _mission(s, user, co, ids, "季度增长方案", _subtasks(ids, 2))
        s.add(m1)
        await s.flush()
        first = await author_mission_workflow(s, m1, agent)
        await s.commit()

        m2 = _mission(s, user, co, ids, "季度增长方案", _subtasks(ids, 2))
        s.add(m2)
        await s.flush()
        again = await author_mission_workflow(s, m2, agent)
        await s.commit()
        assert again.id == first.id
        assert m2.workflow_id == first.id
        total = (await s.execute(select(WorkflowDefinition).where(
            WorkflowDefinition.reuse_key == "季度增长方案",
            WorkflowDefinition.company_id == co["id"]))).scalars().all()
        assert len(total) == 1


async def test_feedback_endpoint_records_and_optimizes(
        client, make_user, make_company):
    user, co, ids = await _setup(client, make_user, make_company)
    saved = await client.post("/workflows", params={"company_id": co["id"]}, json={
        "name": "人工复核流程",
        "nodes": [
            {"node_key": "start", "node_type": "START", "name": "开始"},
            {"node_key": "n1", "node_type": "AGENT", "name": "写初稿",
             "config": {"agent_id": ids["小李"], "instruction": "按模板写初稿"}},
            {"node_key": "end", "node_type": "END", "name": "结束"},
        ],
        "edges": [
            {"from_node": "start", "to_node": "n1"},
            {"from_node": "n1", "to_node": "end"},
        ],
    }, headers=user["headers"])
    assert saved.status_code == 201, saved.text
    wid = saved.json()["id"]

    r = await client.post(f"/workflows/{wid}/feedback", json={
        "score": 40, "lessons": ["必须二次核对数据"],
        "note": "老板手动验收"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feedback"]["score"] == 40
    assert body["optimized_from"] == wid
    new_id = body["workflow"]["id"]
    assert new_id != wid and body["workflow"]["version"] == 2

    detail = await client.get(f"/workflows/{new_id}", headers=user["headers"])
    assert detail.status_code == 200, detail.text
    node = next(n for n in detail.json()["nodes"] if n["node_key"] == "n1")
    assert "必须二次核对数据" in node["config"]["instruction"]
    old = await client.get(f"/workflows/{wid}", headers=user["headers"])
    assert old.json()["feedback"][0]["lessons"] == ["必须二次核对数据"]
