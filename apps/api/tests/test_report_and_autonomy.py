"""交付出口（M4-A/B）：单文件报告与人工介入账本。"""
import pytest

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.report.renderer import render_execution_report
from app.services.autonomy import compute_autonomy, merge_autonomy

GOOD = """## 结论
建议先做小范围试点。

## 依据
来源：https://example.com/a
""" + "有效内容。" * 80


def test_report_is_self_contained_and_escaped():
    html = render_execution_report(
        workflow_name="季度复盘",
        execution={"id": "abcdef1234", "status": "COMMITTED", "error": None},
        steps=[
            {"node_key": "a", "node_name": "写方案", "node_type": "AGENT",
             "status": "COMPLETED", "attempt": 1,
             "output_data": {"output": "<script>alert(1)</script>"},
             "started_at": "2026-11-20T10:00:00+00:00",
             "completed_at": "2026-11-20T10:00:30+00:00", "stale_at": None},
        ],
        gates=[{"node_key": "a", "node_name": "写方案", "attempt": 1,
                "gate_round": 0, "stage": "mechanical", "passed": False,
                "degraded": False, "score": None,
                "checks": [{"rule": "min_chars", "expected": 300, "actual": 12,
                            "passed": False}]}],
        autonomy={"ai_steps": 1, "human_steps": 0, "manual_logs": 1,
                  "autonomy_rate": 0.5, "total": 2},
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="http' not in html and 'src="http' not in html
    assert "<link" not in html and "@import" not in html
    assert "验收结果" in html and "min_chars" in html
    assert "自主率" in html and "50%" in html
    assert "断网双击" in html


def test_report_failed_gates_use_final_effective_attempt_and_round():
    gates = [
        {"node_key": "fixed", "attempt": 1, "gate_round": 0, "passed": False},
        {"node_key": "still-bad", "attempt": 1, "gate_round": 1, "passed": True},
        {"node_key": "fixed", "attempt": 1, "gate_round": 1, "passed": True},
        {"node_key": "still-bad", "attempt": 2, "gate_round": 0, "passed": False},
        {"node_key": "fixed", "attempt": 1, "gate_round": 0, "passed": False},
    ]
    html = render_execution_report(
        workflow_name="乱序闸门", execution={"status": "FAILED"},
        steps=[], gates=gates)
    assert "闸门未过 1</span>" in html
    assert "闸门未过 3</span>" not in html
    reversed_html = render_execution_report(
        workflow_name="乱序闸门", execution={"status": "FAILED"},
        steps=[], gates=list(reversed(gates)))
    assert "闸门未过 1</span>" in reversed_html


def test_report_first_rework_round_one_clears_historical_failure():
    html = render_execution_report(
        workflow_name="返工通过", execution={"status": "COMPLETED"}, steps=[],
        gates=[
            {"node_key": "a", "attempt": 1, "gate_round": 0, "passed": False},
            {"node_key": "a", "attempt": 1, "gate_round": 1, "passed": True},
        ])
    assert "闸门未过 0</span>" in html


def test_autonomy_counts_only_model_agents_and_ignores_stale():
    steps = [
        {"node_key": "a", "node_type": "AGENT", "status": "COMPLETED", "stale_at": None},
        {"node_key": "http", "node_type": "HTTP", "status": "COMPLETED", "stale_at": None},
        {"node_key": "tool", "node_type": "TOOL", "status": "COMPLETED", "stale_at": None},
        {"node_key": "transform", "node_type": "TRANSFORM", "status": "COMPLETED", "stale_at": None},
        {"node_key": "child", "node_type": "SUB_WORKFLOW", "status": "COMPLETED", "stale_at": None},
        {"node_key": "wait", "node_type": "WAIT", "status": "COMPLETED", "stale_at": None},
        {"node_key": "human", "node_type": "APPROVAL", "status": "COMPLETED", "stale_at": None},
        {"node_key": "a", "node_type": "AGENT", "status": "COMPLETED", "stale_at": "2026-11-20"},
        {"node_key": "failed", "node_type": "AGENT", "status": "FAILED", "stale_at": None},
    ]
    a = compute_autonomy(steps, [{"reason": "quality"}, {"reason": "judgment"}])
    assert a["ai_steps"] == 1          # 只有模型驱动的 AGENT 算 AI 工作
    assert a["human_steps"] == 1
    assert a["manual_logs"] == 2
    assert a["autonomy_rate"] == pytest.approx(1 / 4)
    assert a["manual_reasons"] == {"quality": 1, "judgment": 1}

    empty = compute_autonomy([], [])
    assert empty["autonomy_rate"] is None      # 没样本不能显示 0%


def test_autonomy_deduplicates_parent_child_agent_work_by_task():
    steps = [
        {"execution_id": "parent", "node_key": "call", "node_type": "SUB_WORKFLOW",
         "status": "COMPLETED", "stale_at": None,
         "output_data": {"sub_execution": "child"}},
        {"execution_id": "child", "node_key": "writer", "node_type": "AGENT",
         "status": "COMPLETED", "stale_at": None, "attempt": 1,
         "output_data": {"task_id": "task-1", "output": "done"}},
        # 父子流程聚合视图里同一条模型工作可能出现两次。
        {"execution_id": "parent-view", "node_key": "writer", "node_type": "AGENT",
         "status": "COMPLETED", "stale_at": None, "attempt": 1,
         "output_data": {"task_id": "task-1", "output": "done"}},
        {"execution_id": "child", "node_key": "review", "node_type": "AGENT",
         "status": "COMPLETED", "stale_at": None, "attempt": 1,
         "output_data": {"task_id": "task-2", "output": "reviewed"}},
    ]
    ledger = compute_autonomy(steps, [])
    assert ledger["ai_steps"] == 2
    assert ledger["total"] == 2
    assert ledger["autonomy_rate"] == 1


def test_autonomy_does_not_merge_same_node_key_without_identity():
    ledger = compute_autonomy([
        {"node_key": "writer", "node_type": "AGENT", "status": "COMPLETED"},
        {"node_key": "writer", "node_type": "AGENT", "status": "COMPLETED"},
    ], [])
    assert ledger["ai_steps"] == 2


def test_merge_autonomy_adds_denominators_not_averages():
    parts = [{"ai_steps": 9, "human_steps": 1},
             {"ai_steps": 1, "human_steps": 9}]
    merged = merge_autonomy(parts, [{"reason": "external"}])
    assert merged["ai_steps"] == 10 and merged["human_steps"] == 10
    assert merged["autonomy_rate"] == pytest.approx(10 / 21)


async def _seed_execution(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        wf = WorkflowDefinition(company_id=co["id"], name="报告测试",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        a = Agent(company_id=co["id"], name="写手")
        db.add(a)
        await db.flush()
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="start",
                         node_type=NodeType.START, name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="a",
                         node_type=NodeType.AGENT, name="写方案",
                         config={"agent_id": a.id}),
            WorkflowNode(workflow_id=wf.id, node_key="human",
                         node_type=NodeType.APPROVAL, name="审批"),
            WorkflowNode(workflow_id=wf.id, node_key="end",
                         node_type=NodeType.END, name="结束"),
        ])
        for f, t in [("start", "a"), ("a", "human"), ("human", "end")]:
            db.add(WorkflowEdge(workflow_id=wf.id, from_node=f, to_node=t))
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.COMPLETED,
                               started_by=user["id"], current_nodes=["end"],
                               input_data={})
        db.add(ex)
        await db.flush()
        db.add_all([
            WorkflowStep(execution_id=ex.id, node_key="a",
                         node_type=NodeType.AGENT, status=RunStatus.COMPLETED,
                         attempt=1, output_data={"output": GOOD},
                         started_at="2026-11-20T10:00:00+00:00",
                         completed_at="2026-11-20T10:00:30+00:00"),
            WorkflowStep(execution_id=ex.id, node_key="human",
                         node_type=NodeType.APPROVAL, status=RunStatus.COMPLETED,
                         attempt=1, output_data={"approved": True}),
        ])
        await db.commit()
    return user, co, ex.id


@pytest.mark.asyncio
async def test_report_endpoint_and_manual_log_autonomy(
        client, make_user, make_company):
    user, co, eid = await _seed_execution(client, make_user, make_company)
    h = user["headers"]

    r = await client.get(f"/executions/{eid}/report.html", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "报告测试" in r.text and "写方案" in r.text

    base = await client.get(f"/executions/{eid}/autonomy", headers=h)
    assert base.status_code == 200
    assert base.json()["ai_steps"] == 1
    assert base.json()["human_steps"] == 1
    assert base.json()["autonomy_rate"] == 0.5

    log = await client.post(f"/executions/{eid}/manual-log", headers=h, json={
        "node_key": "a", "reason": "quality", "minutes": 20,
        "note": "重写了结论"})
    assert log.status_code == 201

    after = (await client.get(f"/executions/{eid}/autonomy", headers=h)).json()
    assert after["manual_logs"] == 1
    assert after["autonomy_rate"] == pytest.approx(1 / 3)

    bad = await client.post(f"/executions/{eid}/manual-log", headers=h, json={
        "reason": "因为我觉得"})
    assert bad.status_code == 400

    company = (await client.get(
        f"/metrics/autonomy?company_id={co['id']}", headers=h)).json()
    assert company["ai_steps"] == 1 and company["manual_logs"] == 1
    report2 = await client.get(f"/executions/{eid}/report.html", headers=h)
    assert "33%" in report2.text
