"""经验池：失败门槛、成功工作流、跨 Agent 注入、采纳与租户隔离。

模型提炼默认关闭（环境变量），验的是抽取、复用与隔离逻辑本身。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app import experience_engine as exp
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.experience import SharedExperience
from app.models.task import AgentExecution, Task, TaskStatus
from app.models.token_usage import TokenUsage
from app.models.workflow import WorkflowDefinition, WorkflowNode
from app.schemas.responses_experience import (ExperienceOut,
                                              ExperienceOverviewOut,
                                              ExperienceStatsOut)


def _assert_keys(body: dict, model) -> None:
    assert set(body) == set(model.model_fields), (
        f"{model.__name__} 键集合不一致：多出 "
        f"{sorted(set(body) - set(model.model_fields))}，缺少 "
        f"{sorted(set(model.model_fields) - set(body))}")


async def _agent(company_id: str, user_id: str, name: str) -> str:
    async with AsyncSessionLocal() as s:
        a = Agent(id=str(uuid.uuid4()), company_id=company_id, name=name,
                  role="执行专员", status=AgentStatus.IDLE,
                  created_by_user_id=user_id)
        s.add(a)
        await s.commit()
        return a.id


async def _task(company_id: str, user_id: str, agent_id: str,
                title: str = "整理季度经营分析",
                status: TaskStatus = TaskStatus.COMPLETED,
                result: str | None = None) -> str:
    async with AsyncSessionLocal() as s:
        t = Task(id=str(uuid.uuid4()), company_id=company_id, title=title,
                 status=status, assigned_agent_id=agent_id,
                 created_by=user_id, result=result)
        s.add(t)
        await s.commit()
        return t.id


async def _execution(task_id: str, agent_id: str, status: str,
                     output: str | None = None,
                     error: str | None = None) -> None:
    async with AsyncSessionLocal() as s:
        s.add(AgentExecution(id=str(uuid.uuid4()), agent_id=agent_id,
                             task_id=task_id, status=status,
                             output=output, error=error))
        await s.commit()


async def _tokens(task_id: str, company_id: str, agent_id: str,
                  total: int) -> None:
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(id=str(uuid.uuid4()), company_id=company_id,
                         agent_id=agent_id, task_id=task_id,
                         scenario="TASK", total_tokens=total))
        await s.commit()


async def _harvest(task_id: str, agent_id: str, *, success: bool,
                   error: str | None = None) -> dict | None:
    async with AsyncSessionLocal() as s:
        task = await s.get(Task, task_id)
        agent = await s.get(Agent, agent_id)
        return await exp.harvest_task_experience(
            s, task, agent, success=success, error=error)


def _no_llm(monkeypatch):
    monkeypatch.setenv("AIEOS_EXPERIENCE_LLM", "0")


# ---------------------------------------------------------------------------
# 沉淀门槛
# ---------------------------------------------------------------------------

async def test_single_failure_is_not_published(tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    agent_id = await _agent(co, user, "小李")
    task_id = await _task(co, user, agent_id, status=TaskStatus.FAILED)
    await _execution(task_id, agent_id, "failed", error="上游接口 500")

    assert await _harvest(task_id, agent_id, success=False,
                          error="上游接口 500") is None
    async with AsyncSessionLocal() as s:
        count = (await s.execute(
            select(SharedExperience)
            .where(SharedExperience.task_id == task_id))).scalars().all()
    assert count == []


async def test_repeated_failure_publishes_lesson(tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    agent_id = await _agent(co, user, "小李")
    task_id = await _task(co, user, agent_id, status=TaskStatus.FAILED)
    await _execution(task_id, agent_id, "failed", error="缺少客户数据")
    await _execution(task_id, agent_id, "failed", error="缺少客户数据")
    await _tokens(task_id, co, agent_id, 1200)

    rec = await _harvest(task_id, agent_id, success=False,
                         error="缺少客户数据")
    assert rec is not None
    assert rec["kind"] == "failure"
    assert rec["attempts"] == 2 and rec["failure_count"] == 2
    assert rec["tokens_used"] == 1200
    assert rec["tokens_saved_est"] == 1200
    assert rec["workflow_id"] is None
    assert any("缺少客户数据" in p for p in rec["pitfalls"])
    async with AsyncSessionLocal() as s:
        row = await s.get(SharedExperience, rec["id"])
        assert row.status == "published" and row.scope == "company"


async def test_failure_then_success_creates_reusable_workflow(
        tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    agent_id = await _agent(co, user, "小李")
    task_id = await _task(
        co, user, agent_id, status=TaskStatus.COMPLETED,
        result="## 第一步 对齐口径\n## 第二步 汇总数据\n## 第三步 复核结论")
    await _execution(task_id, agent_id, "failed", error="口径未对齐")
    await _execution(task_id, agent_id, "failed", error="口径未对齐")
    await _execution(task_id, agent_id, "completed",
                     output="## 第一步 对齐口径\n## 第二步 汇总数据")

    rec = await _harvest(task_id, agent_id, success=True)
    assert rec is not None
    assert rec["kind"] == "success"
    assert rec["attempts"] == 3 and rec["failure_count"] == 2
    assert rec["steps"], "成功经验必须有可执行步骤"
    assert rec["workflow_id"] and rec["workflow_version"] == 1
    assert rec["confidence"] >= 0.8

    async with AsyncSessionLocal() as s:
        wf = await s.get(WorkflowDefinition, rec["workflow_id"])
        assert wf.source == "experience"
        assert wf.reuse_key == rec["reuse_key"]
        nodes = (await s.execute(
            select(WorkflowNode)
            .where(WorkflowNode.workflow_id == wf.id))).scalars().all()
        assert len(nodes) == len(rec["steps"]) + 2  # START + 步骤 + END

    again = await _harvest(task_id, agent_id, success=True)
    assert again["id"] == rec["id"], "同一任务重复上报必须幂等"
    async with AsyncSessionLocal() as s:
        count = (await s.execute(
            select(SharedExperience)
            .where(SharedExperience.task_id == task_id))).scalars().all()
    assert len(count) == 1


async def test_single_success_baseline_publishes_once_per_reuse_key(
        tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    agent_id = await _agent(co, user, "小李")
    first = await _task(co, user, agent_id, result="产出已完成")
    await _execution(first, agent_id, "completed", output="产出已完成")
    rec = await _harvest(first, agent_id, success=True)
    assert rec is not None and rec["attempts"] == 1

    second = await _task(co, user, agent_id, result="产出已完成")
    await _execution(second, agent_id, "completed", output="产出已完成")
    assert await _harvest(second, agent_id, success=True) is None


# ---------------------------------------------------------------------------
# 跨 Agent 学习
# ---------------------------------------------------------------------------

async def test_experience_context_is_injected_for_colleagues(
        tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    author = await _agent(co, user, "小李")
    learner = await _agent(co, user, "小王")
    task_id = await _task(co, user, author, result="## 对齐口径\n## 汇总数据")
    await _execution(task_id, author, "failed", error="口径未对齐")
    await _execution(task_id, author, "failed", error="口径未对齐")
    await _execution(task_id, author, "completed", output="## 对齐口径\n## 汇总数据")
    await _harvest(task_id, author, success=True)

    new_task_id = await _task(co, user, learner, status=TaskStatus.TODO,
                              title="整理季度经营分析")
    async with AsyncSessionLocal() as s:
        task = await s.get(Task, new_task_id)
        ctx = await exp.build_experience_context(
            s, co, learner, task)
    assert "已验证工作流" in ctx
    assert "团队经验" in ctx and "小李" in ctx
    assert "对齐口径" in ctx

    async with AsyncSessionLocal() as s:
        task = await s.get(Task, new_task_id)
        other = await exp.build_experience_context(
            s, tenants.bob_co["id"], learner, task)
    assert other == "", "别的公司不能看到这家公司的经验"


async def test_adopt_experience_writes_memory_and_tracks_adopter(
        tenants, monkeypatch):
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    author = await _agent(co, user, "小李")
    learner = await _agent(co, user, "小王")
    task_id = await _task(co, user, author, result="## 对齐口径")
    await _execution(task_id, author, "failed", error="口径未对齐")
    await _execution(task_id, author, "failed", error="口径未对齐")
    rec = await _harvest(task_id, author, success=True)

    async with AsyncSessionLocal() as s:
        row = await s.get(SharedExperience, rec["id"])
        out = await exp.adopt_experience(s, row, learner, user_id=user)
    assert learner in out["adopted_by"]

    from app.models.memory import AgentMemory
    async with AsyncSessionLocal() as s:
        mems = (await s.execute(
            select(AgentMemory)
            .where(AgentMemory.agent_id == learner))).scalars().all()
    assert any("学习同事经验" in (m.content or "") for m in mems)

    async with AsyncSessionLocal() as s:
        row = await s.get(SharedExperience, rec["id"])
        try:
            await exp.adopt_experience(
                s, row, "not-an-agent", user_id=user)
        except ValueError as exc:
            assert "不存在" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("不存在的员工不能采纳经验")


# ---------------------------------------------------------------------------
# HTTP 契约与租户隔离
# ---------------------------------------------------------------------------

async def test_experience_http_contract_and_tenant_isolation(
        tenants, monkeypatch, client):
    _no_llm(monkeypatch)
    alice, bob = tenants.alice, tenants.bob
    co = alice["company_id"] if "company_id" in alice else tenants.alice_co["id"]
    agent_id = await _agent(co, alice["id"], "小李")
    task_id = await _task(co, alice["id"], agent_id,
                          status=TaskStatus.COMPLETED, result="## 完成")
    await _execution(task_id, agent_id, "completed", output="## 完成")
    rec = await _harvest(task_id, agent_id, success=True)

    r = await client.get(f"/experiences?company_id={co}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == set(ExperienceOverviewOut.model_fields)
    _assert_keys(body["stats"], ExperienceStatsOut)
    _assert_keys(body["items"][0], ExperienceOut)
    assert body["items"][0]["author_agent_name"] == "小李"

    r = await client.get(f"/experiences/{rec['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200
    _assert_keys(r.json(), ExperienceOut)

    r = await client.get(f"/experiences/{rec['id']}", headers=bob["headers"])
    assert r.status_code == 404
    r = await client.get(f"/experiences?company_id={co}",
                         headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post(f"/experiences/{rec['id']}/adopt",
                          json={"agent_id": agent_id},
                          headers=bob["headers"])
    assert r.status_code == 404

    # 未结束的任务不能提炼
    todo = await _task(co, alice["id"], agent_id, status=TaskStatus.TODO)
    r = await client.post("/experiences/harvest",
                          json={"company_id": co, "task_id": todo},
                          headers=alice["headers"])
    assert r.status_code == 400

    # 采纳给别家公司的员工：拒绝
    other_agent = await _agent(tenants.bob_co["id"], bob["id"], "外公司员工")
    r = await client.post(f"/experiences/{rec['id']}/adopt",
                          json={"agent_id": other_agent},
                          headers=alice["headers"])
    assert r.status_code == 400


async def test_harvest_after_commit_uses_fresh_objects(tenants, monkeypatch):
    """运行时失败路径会先 commit 证据，再沉淀经验。

    此时 ORM 对象已过期，harvest 必须自己按主键重读——否则在异步上下文里
    访问过期属性会冒 MissingGreenlet，经验静默不落库。
    """
    _no_llm(monkeypatch)
    co, user = tenants.alice_co["id"], tenants.alice["id"]
    agent_id = await _agent(co, user, "小李")
    task_id = await _task(co, user, agent_id, status=TaskStatus.FAILED)
    await _execution(task_id, agent_id, "failed", error="接口 502")
    await _execution(task_id, agent_id, "failed", error="接口 502")

    async with AsyncSessionLocal() as s:
        task = await s.get(Task, task_id)
        agent = await s.get(Agent, agent_id)
        await s.commit()  # 模拟运行时：证据先落库，属性全部过期
        rec = await exp.harvest_task_experience(
            s, task, agent, success=False, error="接口 502")

    assert rec is not None and rec["kind"] == "failure"
