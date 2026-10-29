"""首页/任务详情的"轮询视图"：轻量、有界、带归属校验。

## 为什么这组用例存在

P1/P2 把首页与任务详情的轮询从"全量串行"改成"单端点 + 有界字段"。
改动的风险不在功能本身，而在**边界**：
  * bootstrap 里的"运行中任务"必须 limit，否则又把全表拉回来了；
  * summary 必须不带 result 全文、output 必须截断，否则轮询依旧搬大 payload；
  * 两个新端点都必须与老端点同一套归属校验（不能新开一个越权口）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _agent_and_task(client, user, company) -> tuple[str, str]:
    r = await client.post("/agents", json={
        "company_id": company["id"], "name": "轮询视图员工"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    r = await client.post("/tasks", json={
        "company_id": company["id"], "title": "轮询视图任务",
        "description": "D" * 5000, "assigned_agent_id": agent_id},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return agent_id, r.json()["id"]


async def test_dashboard_bootstrap_is_one_bounded_call(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    _, task_id = await _agent_and_task(client, alice, co)
    # 填一段超长 result：bootstrap 绝不能把它带回来
    from app.db.session import AsyncSessionLocal
    from app.models.task import Task

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, task_id)
        t.result = "R" * 20000
        await s.commit()

    r = await client.get(f"/dashboard/{co['id']}/bootstrap",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"dashboard", "departments_overview", "active_tasks"}
    assert len(body["active_tasks"]) <= 20
    for t in body["active_tasks"]:
        assert "result" not in t and "description" not in t
    # 部门概览与仪表盘都在同一次响应里，前端只需一个 RTT
    assert "departments" in body["departments_overview"]
    assert body["dashboard"]["company"]["id"] == co["id"]


async def test_dashboard_bootstrap_rejects_foreign_company(client, tenants):
    r = await client.get(f"/dashboard/{tenants.alice_co['id']}/bootstrap",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, f"B 读到了 A 的首页数据：{r.status_code} {r.text}"


async def test_task_summary_omits_result_and_truncates_output(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_id, task_id = await _agent_and_task(client, alice, co)

    from app.db.session import AsyncSessionLocal
    from app.models.task import AgentExecution, Task

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, task_id)
        t.result = "RESULT-SHOULD-NOT-APPEAR"
        s.add(AgentExecution(agent_id=agent_id, task_id=task_id,
                             status="completed", output="O" * 9000))
        await s.commit()

    r = await client.get(f"/tasks/{task_id}/summary", headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert "result" not in body, "summary 仍然返回了 result 全文"
    assert "RESULT-SHOULD-NOT-APPEAR" not in r.text
    ex = body["executions"][0]
    assert len(ex["output"]) <= 2048, f"execution 输出未截断：{len(ex['output'])}"
    assert ex["output_truncated"] is True
    assert body["executions_total"] == 1


async def test_task_summary_rejects_foreign_task(client, tenants):
    _, task_id = await _agent_and_task(client, tenants.alice, tenants.alice_co)
    r = await client.get(f"/tasks/{task_id}/summary",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, f"B 读到了 A 的任务摘要：{r.status_code} {r.text}"
