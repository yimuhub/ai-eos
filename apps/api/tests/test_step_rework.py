"""步骤级返工（M3-A）：整轮 / 从某步 / 带意见在原稿上改。

不调真实模型：节点执行用假函数返回固定文本，测试关心的是
"谁重跑了、跑了几次、上游有没有被白烧 token"。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.workflow_runtime import (WorkflowRuntime, advance_execution,
                                  build_revise_feedback, node_input_signature)

CALLS: dict[str, int] = {}
NEXT: dict[str, str] = {}


def reset_state():
    CALLS.clear()
    NEXT.clear()


async def fake_exec(self, node, ctx, gate_feedback=None):
    key = node.node_key
    CALLS[key] = CALLS.get(key, 0) + 1
    return {"output": NEXT.get(key) or f"{key}-初版"}


async def _build_chain(client, make_user, make_company):
    """START → A(AGENT) → END，用来验证带意见返工的真实任务文本。"""
    reset_state()
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        wf = WorkflowDefinition(company_id=co["id"], name="链式返工测试",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        agent = Agent(company_id=co["id"], name="写手")
        db.add(agent)
        await db.flush()
        db.add_all([
            WorkflowNode(workflow_id=wf.id, node_key="start",
                         node_type=NodeType.START, name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="a",
                         node_type=NodeType.AGENT, name="写方案",
                         config={"agent_id": agent.id,
                                 "instruction": "写一份方案"}),
            WorkflowNode(workflow_id=wf.id, node_key="end",
                         node_type=NodeType.END, name="结束"),
        ])
        db.add_all([
            WorkflowEdge(workflow_id=wf.id, from_node="start", to_node="a"),
            WorkflowEdge(workflow_id=wf.id, from_node="a", to_node="end"),
        ])
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.PENDING,
                               started_by=user["id"], current_nodes=["start"],
                               input_data={"company_id": co["id"]})
        db.add(ex)
        await db.commit()
        return SimpleNamespace(wf=wf, ex=ex, user=user, company=co,
                               agent=agent, eid=ex.id)


async def _build_diamond(client, make_user, make_company):
    """START → A → (B ∥ C) → D → END，菱形依赖：D 的上游有两条路。"""
    reset_state()
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        wf = WorkflowDefinition(company_id=co["id"], name="菱形返工测试",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        agent = Agent(company_id=co["id"], name="执行者")
        db.add(agent)
        await db.flush()
        cfg = {"agent_id": agent.id}
        for key, ntype in [("start", NodeType.START), ("a", NodeType.AGENT),
                           ("b", NodeType.AGENT), ("c", NodeType.AGENT),
                           ("d", NodeType.AGENT), ("end", NodeType.END)]:
            db.add(WorkflowNode(workflow_id=wf.id, node_key=key, node_type=ntype,
                                name=key, config=cfg if ntype == NodeType.AGENT else {}))
        for f, t in [("start", "a"), ("a", "b"), ("a", "c"),
                     ("b", "d"), ("c", "d"), ("d", "end")]:
            db.add(WorkflowEdge(workflow_id=wf.id, from_node=f, to_node=t))
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.PENDING,
                               started_by=user["id"], current_nodes=["start"],
                               input_data={"company_id": co["id"]})
        db.add(ex)
        await db.commit()
        return SimpleNamespace(wf=wf, ex=ex, user=user, company=co,
                               agent=agent, eid=ex.id)


async def _advance(eid: str):
    async with AsyncSessionLocal() as db:
        await advance_execution(db, eid)
    async with AsyncSessionLocal() as db:
        return await db.get(WorkflowExecution, eid)


async def _rework(client, eid, headers, **body):
    return await client.post(f"/executions/{eid}/rework", json=body,
                             headers=headers)


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def test_signature_changes_with_upstream_output():
    node = WorkflowNode(node_key="b", node_type=NodeType.AGENT, name="B",
                        config={"agent_id": "x", "instruction": "做"})
    s1 = node_input_signature(node, ["a"], {"a": {"output": "v1"}})
    s2 = node_input_signature(node, ["a"], {"a": {"output": "v2"}})
    s3 = node_input_signature(node, ["a"], {"a": {"output": "v1"}})
    assert s1 != s2
    assert s1 == s3          # 同样输入 → 同样指纹，才能复用


def test_signature_ignores_transient_config_keys():
    base = dict(node_key="a", node_type=NodeType.APPROVAL, name="审批")
    s1 = node_input_signature(WorkflowNode(**base, config={}), ["s"], {"s": {}})
    s2 = node_input_signature(WorkflowNode(**base, config={"_approved": True}),
                              ["s"], {"s": {}})
    assert s1 == s2


def test_revise_feedback_shape():
    text = build_revise_feedback("上一版正文", "结尾太平淡")
    assert "上一版正文" in text and "结尾太平淡" in text
    assert "只针对上述问题在原稿上修改" in text
    assert "不要解释你改了什么" in text
    # 超长原稿要截断，不能把整个历史产出塞进提示词
    assert len(build_revise_feedback("x" * 20000, "改")) < 13000


# ---------------------------------------------------------------------------
# 返工行为
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rework_middle_node_skips_upstream_and_runs_diamond_once(
        client, make_user, make_company, monkeypatch):
    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_exec)
    s = await _build_diamond(client, make_user, make_company)
    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    base = dict(CALLS)
    assert base == {"a": 1, "b": 1, "c": 1, "d": 1}

    NEXT["b"] = "b-第二版"                     # B 的产出变了
    resp = await _rework(client, s.eid, s.user["headers"], node_key="b", mode="rerun")
    assert resp.status_code == 202

    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    assert CALLS["a"] == base["a"]              # 上游 0 次，token 不白烧
    assert CALLS["b"] == base["b"] + 1
    assert CALLS["c"] == base["c"]              # C 只依赖 A：指纹没变 → 复用
    assert CALLS["d"] == base["d"] + 1          # D 只跑一次（不是两条边各跑一次）


@pytest.mark.asyncio
async def test_rework_with_same_output_reuses_downstream(
        client, make_user, make_company, monkeypatch):
    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_exec)
    s = await _build_diamond(client, make_user, make_company)
    await _advance(s.eid)
    base = dict(CALLS)

    # 重跑 B，但产出和上一版一模一样 → D 的输入指纹不变 → D 不该再跑
    resp = await _rework(client, s.eid, s.user["headers"], node_key="b", mode="rerun")
    assert resp.status_code == 202
    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    assert CALLS["b"] == base["b"] + 1
    assert CALLS["d"] == base["d"]
    assert CALLS.get("end", 0) == 0


@pytest.mark.asyncio
async def test_full_rerun_invalidates_everything(client, make_user, make_company,
                                                 monkeypatch):
    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_exec)
    s = await _build_diamond(client, make_user, make_company)
    await _advance(s.eid)
    base = dict(CALLS)

    resp = await _rework(client, s.eid, s.user["headers"], mode="rerun")
    assert resp.status_code == 202
    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    for key in ("a", "b", "c", "d"):
        assert CALLS[key] == base[key] + 1, key

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == s.eid))).scalars().all()
    assert {r.node_key for r in rows} == {"start", "a", "b", "c", "d", "end"}
    # 每个 AGENT 节点都开了新 attempt，旧 attempt 原样保留
    assert max(r.attempt for r in rows if r.node_key == "a") == 2


@pytest.mark.asyncio
async def test_revise_sends_previous_output_and_feedback_to_agent(
        client, make_user, make_company, monkeypatch):
    s = await _build_chain(client, make_user, make_company)
    seen: list[str] = []

    async def fake_run_agent_on_task(db, agent, task, actor_type="system",
                                     takeover_note=None, extra_system=None):
        from app.models.task import TaskStatus
        seen.append(task.title)
        task.status = TaskStatus.RUNNING      # 真实 runtime 会推进状态机，假函数补这一步
        task.result = "初版内容：……结尾就此结束。"
        return None

    monkeypatch.setattr("app.agent_runtime.run_agent_on_task",
                        fake_run_agent_on_task)

    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    first_title = seen[-1]

    # 先让"上一版"有可回交的正文
    async with AsyncSessionLocal() as db:
        step = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == s.eid,
            WorkflowStep.node_key == "a"))).scalars().one()
        step.output_data = {"task_id": "t1", "output": "初版内容：……结尾就此结束。"}
        await db.commit()

    resp = await _rework(client, s.eid, s.user["headers"], node_key="a",
                   mode="revise", feedback="结尾太平淡，加一句反转型收尾")
    assert resp.status_code == 202
    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED

    assert "（改稿）" in seen[-1] and "（改稿）" not in first_title
    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == s.eid,
            WorkflowStep.node_key == "a"))).scalars().all()
        from app.models.task import Task
        task = (await db.execute(select(Task).where(
            Task.title.like("%改稿%")))).scalars().one()
    assert "【上一版产出】" in task.description
    assert "初版内容：……结尾就此结束。" in task.description
    assert "结尾太平淡，加一句反转型收尾" in task.description
    assert "只针对上述问题在原稿上修改" in task.description
    assert max(t.attempt for t in tasks) == 2
    # 旧版本保留且被标记作废，历史可查、可对比
    assert any(t.stale_at for t in tasks if t.attempt == 1)


@pytest.mark.asyncio
async def test_rework_recovers_failed_execution(client, make_user, make_company,
                                                monkeypatch):
    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_exec)
    s = await _build_diamond(client, make_user, make_company)
    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, s.eid)
        ex.status = RunStatus.FAILED
        ex.error = "node b: boom"
        await db.commit()

    resp = await _rework(client, s.eid, s.user["headers"], node_key="b", mode="rerun")
    assert resp.status_code == 202
    ex = await _advance(s.eid)
    assert ex.status == RunStatus.COMPLETED
    assert ex.error is None


# ---------------------------------------------------------------------------
# 接口校验
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rework_rejects_bad_requests(client, make_user, make_company,
                                           monkeypatch):
    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_exec)
    s = await _build_chain(client, make_user, make_company)
    await _advance(s.eid)
    h = s.user["headers"]

    assert (await _rework(client, s.eid, h, node_key="nope")).status_code == 404
    assert (await _rework(client, s.eid, h, node_key="end")).status_code == 400
    assert (await _rework(client, s.eid, h, node_key="start")).status_code == 400
    assert (await _rework(client, s.eid, h, mode="bogus")).status_code == 400
    assert (await _rework(client, s.eid, h, mode="revise",
                          node_key="a")).status_code == 400
    assert (await _rework(client, s.eid, h, mode="revise",
                          feedback="改")).status_code == 400

    async with AsyncSessionLocal() as db:
        ex = await db.get(WorkflowExecution, s.eid)
        ex.status = RunStatus.RUNNING
        await db.commit()
    assert (await _rework(client, s.eid, h, node_key="a")).status_code == 409
