"""验收闸门端到端：断言 → 证据落库 → 阻断/放行 → 定向返工。

不调真实模型：评审分支只验证"不可用时降级放行"，模型评审本身
（返回 JSON 的解析与阈值判定）用假返回值单测覆盖。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.gates.step_gate import GateBlocked, enforce, resolve_gate
from app.models.agent import Agent
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.models.workflow_gate import WorkflowStepGate
from app.workflow_runtime import WorkflowRuntime, advance_execution

# 合格产出：2 个文件块 + 足够有效字
GOOD = """## 方案

这是经过分析后的完整方案说明，覆盖了目标、路径与成本三部分内容。
来源：https://example.com/report

### 文件：src/app.py
```python
print("ok")
```

【文件：README.md】
使用说明
"""

# 缺件产出：只有 1 个文件块
BAD = """## 方案

这是经过分析后的方案说明，覆盖了目标、路径与成本三部分内容。
来源：https://example.com/report

### 文件：src/app.py
```python
print("ok")
```
"""

GATE = {
    "assert": {"min_chars": 40, "emits_files": 2, "min_citations": 1},
    "max_rework": 1,
}


async def _build(client, make_user, make_company, gate: dict | None = None,
                 with_end: bool = True):
    """建一张 START→A(AGENT)→END 的图 + 一个 PENDING 执行实例。"""
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        wf = WorkflowDefinition(company_id=co["id"], name="闸门测试",
                                created_by=user["id"])
        db.add(wf)
        await db.flush()
        # 真实员工行：闸门返工要靠"该节点的执行员工"回炉，没有员工就只能阻断
        agent = Agent(company_id=co["id"], name="写手")
        db.add(agent)
        await db.flush()
        agent_cfg = {"agent_id": agent.id}
        nodes = [
            WorkflowNode(workflow_id=wf.id, node_key="start",
                         node_type=NodeType.START, name="开始"),
            WorkflowNode(workflow_id=wf.id, node_key="a",
                         node_type=NodeType.AGENT, name="写方案",
                         config={**agent_cfg, **({"gate": gate} if gate else {})}),
        ]
        if with_end:
            nodes.append(WorkflowNode(workflow_id=wf.id, node_key="end",
                                      node_type=NodeType.END, name="结束"))
        db.add_all(nodes)
        edges = [WorkflowEdge(workflow_id=wf.id, from_node="start", to_node="a")]
        if with_end:
            edges.append(WorkflowEdge(workflow_id=wf.id, from_node="a", to_node="end"))
        db.add_all(edges)
        ex = WorkflowExecution(workflow_id=wf.id, status=RunStatus.PENDING,
                               started_by=user["id"], current_nodes=["start"],
                               input_data={"company_id": co["id"]})
        db.add(ex)
        await db.commit()
        node = [n for n in nodes if n.node_key == "a"][0]
        return SimpleNamespace(workflow=wf, execution=ex, node=node,
                               company=co, user=user, db_session=db)


async def _gate_rows(execution_id: str) -> list[WorkflowStepGate]:
    async with AsyncSessionLocal() as db:
        q = await db.execute(
            select(WorkflowStepGate)
            .where(WorkflowStepGate.execution_id == execution_id)
            .order_by(WorkflowStepGate.gate_round))
        return list(q.scalars())


# ---------------------------------------------------------------------------
# enforce 单点验证
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_file_block_blocks_and_persists_evidence(client, make_user, make_company):
    ctx = await _build(client, make_user, make_company, GATE)
    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        with pytest.raises(GateBlocked) as ei:
            await enforce(db, execution_id=ctx.execution.id, node=node,
                          output={"output": BAD}, attempt=1)
        await db.commit()
    assert "emits_files" in str(ei.value)

    rows = await _gate_rows(ctx.execution.id)
    assert len(rows) == 1
    assert rows[0].passed is False and rows[0].blocking is True
    failed = {c["rule"] for c in rows[0].checks if not c["passed"]}
    assert failed == {"emits_files"}


@pytest.mark.asyncio
async def test_clean_output_passes_without_model_call(client, make_user, make_company):
    ctx = await _build(client, make_user, make_company, GATE)
    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        out = await enforce(db, execution_id=ctx.execution.id, node=node,
                            output={"output": GOOD}, attempt=1)
        await db.commit()
    assert out.passed and out.enabled and not out.degraded
    rows = await _gate_rows(ctx.execution.id)
    assert len(rows) == 1 and rows[0].passed is True and rows[0].tokens == 0


@pytest.mark.asyncio
async def test_rework_runs_once_and_records_both_rounds(client, make_user, make_company):
    ctx = await _build(client, make_user, make_company, GATE)
    prompts: list[str] = []

    async def rework(prompt: str) -> dict:
        prompts.append(prompt)
        return {"output": GOOD}

    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        out = await enforce(db, execution_id=ctx.execution.id, node=node,
                            output={"output": BAD}, attempt=1, rework=rework)
        await db.commit()

    assert out.passed and out.round == 1
    assert len(prompts) == 1
    # 返工指令必须是"在原稿上改"，并带上具体未过项
    assert "上一版产出" in prompts[0] and "不要重写" in prompts[0]
    assert "emits_files" in prompts[0]

    rows = await _gate_rows(ctx.execution.id)
    assert [r.gate_round for r in rows] == [0, 1]
    assert rows[0].passed is False and rows[1].passed is True
    # 首轮失败的证据不能返工后被覆盖
    assert rows[1].output_before and "src/app.py" in rows[1].output_before


@pytest.mark.asyncio
async def test_rework_still_failing_blocks(client, make_user, make_company):
    ctx = await _build(client, make_user, make_company, GATE)

    async def rework(prompt: str) -> dict:
        return {"output": BAD}  # 改了但没改对

    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        with pytest.raises(GateBlocked):
            await enforce(db, execution_id=ctx.execution.id, node=node,
                          output={"output": BAD}, attempt=1, rework=rework)
        await db.commit()
    rows = await _gate_rows(ctx.execution.id)
    assert len(rows) == 2 and rows[-1].passed is False


@pytest.mark.asyncio
async def test_bad_gate_field_fails_loudly(client, make_user, make_company):
    ctx = await _build(client, make_user, make_company, {"min_chars": 100})
    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        with pytest.raises(ValueError) as ei:
            resolve_gate(node)
    assert "未知字段" in str(ei.value)


# ---------------------------------------------------------------------------
# 执行器联动：闸门是旁路，但失败必须真的阻断 END
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execution_fails_and_end_never_runs(client, make_user, make_company,
                                                  monkeypatch):
    ctx = await _build(client, make_user, make_company, GATE)

    async def fake_agent(self, node, ctx_dict, gate_feedback=None):
        return {"output": BAD}

    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_agent)

    async with AsyncSessionLocal() as db:
        ex = await advance_execution(db, ctx.execution.id)

    assert ex.status == RunStatus.FAILED, f"意外成功: {ex.status}"
    assert "验收未通过" in (ex.error or ""), ex.error

    async with AsyncSessionLocal() as db:
        steps = (await db.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == ctx.execution.id))).scalars().all()
        keys = {s.node_key for s in steps}
        statuses = {s.node_key: s.status for s in steps}
    assert "end" not in keys           # 缺件不放行：END 根本没执行
    assert statuses["a"] == RunStatus.FAILED


@pytest.mark.asyncio
async def test_execution_completes_when_rework_fixes_it(client, make_user,
                                                        make_company, monkeypatch):
    ctx = await _build(client, make_user, make_company, GATE)

    async def fake_agent(self, node, ctx_dict, gate_feedback=None):
        return {"output": GOOD if gate_feedback else BAD}

    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_agent)

    async with AsyncSessionLocal() as db:
        ex = await advance_execution(db, ctx.execution.id)

    assert ex.status == RunStatus.COMPLETED, ex.error
    rows = await _gate_rows(ctx.execution.id)
    assert len(rows) == 2 and rows[-1].passed is True


@pytest.mark.asyncio
async def test_no_gate_means_zero_overhead(client, make_user, make_company, monkeypatch):
    """未配置 gate 的节点行为必须与本版之前完全一致（不加检查、不落证据）。"""
    ctx = await _build(client, make_user, make_company, None)

    async def fake_agent(self, node, ctx_dict, gate_feedback=None):
        return {"output": BAD}  # 即便产出不合规，没有 gate 就不该拦

    monkeypatch.setattr(WorkflowRuntime, "_exec_agent_node", fake_agent)

    async with AsyncSessionLocal() as db:
        ex = await advance_execution(db, ctx.execution.id)

    assert ex.status == RunStatus.COMPLETED, ex.error
    assert await _gate_rows(ctx.execution.id) == []


@pytest.mark.asyncio
async def test_observe_mode_records_but_never_blocks(client, make_user, make_company):
    """上线观察期：只留证、不阻断——这是自动生成的工作流默认取值。"""
    gate = dict(GATE, on_fail="observe")
    ctx = await _build(client, make_user, make_company, gate)
    async with AsyncSessionLocal() as db:
        node = await db.get(WorkflowNode, ctx.node.id)
        out = await enforce(db, execution_id=ctx.execution.id, node=node,
                            output={"output": BAD}, attempt=1)
        await db.commit()
    assert out.enabled and out.passed is False      # 记录为未通过
    rows = await _gate_rows(ctx.execution.id)
    assert rows[0].passed is False and rows[0].blocking is True


def test_sanitize_gate_drops_invented_rules_and_forces_observe():
    from app.workflow_authoring import _sanitize_gate

    assert _sanitize_gate(None) is None
    assert _sanitize_gate({}) is None
    assert _sanitize_gate({"assert": {"min_words": 300}}) is None  # 自创规则全丢 → 关闸

    cleaned = _sanitize_gate({
        "acceptance": "必须给出结论",
        "assert": {"min_chars": 300, "min_words": 300},  # 后者不存在
        "on_fail": "fail",      # 自动生成不允许阻断
        "max_rework": 5,        # 自动生成不允许返工
    })
    assert cleaned["acceptance"] == "必须给出结论"
    assert set(cleaned["assert"]) == {"min_chars"}
    assert cleaned["on_fail"] == "observe"
    assert cleaned["max_rework"] == 0
