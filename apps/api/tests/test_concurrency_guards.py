"""并发重复执行护栏。

覆盖本轮 P0-6 修复的四条关键路径（全部离线、不调真实 LLM、不碰真实库）：

1. 审批单并发裁定：双击/重试/双端同时点「批准」时，真实副作用只执行一次；
2. 任务并发「开始」：只有一个请求能把 TODO 抢成 RUNNING 并提交后台执行；
3. 作战室 kickoff：后台 worker 投递前，会话行必须已经 commit（否则它读不到，
   会话永远卡在 analyzing）；
4. 工作流取消：取消后再次推进不再执行任何节点（不会被"复活"）。

另附会议轮次租约用例：两个 worker 同时推进同一轮，只有一个真正发言。
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db.session import AsyncSessionLocal, engine
from app.models.meeting import Meeting, MeetingMessage, MeetingStatus
from app.models.workflow import RunStatus, WorkflowStep
from app.workflow_runtime import WorkflowRuntime, advance_execution


async def _make_agent(client, user, company_id, name) -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "测试"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1) 审批：并发裁定，副作用只执行一次
# ---------------------------------------------------------------------------
async def test_concurrent_approval_executes_side_effect_once(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await client.post(
        f"/intervention/requests?company_id={co['id']}",
        json={
            "action_type": "computer_command",
            "payload": {"capability": "run_shell",
                        "params": {"cmd": "echo hello"},
                        "awaiting_execution": True},
            "reason": "并发测试",
            "force": True,
        },
        headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("request_id"), body
    request_id = body["request_id"]

    executed = AsyncMock(return_value={"result": {"stdout": "hello"}})
    # 路由在调用时才 import execute_computer_action，因此 patch 源模块即可
    with patch("app.services.computer_service.execute_computer_action", executed):
        responses = await asyncio.gather(*[
            client.post(f"/intervention/requests/{request_id}/decide",
                        json={"decision": "approve"},
                        headers=alice["headers"])
            for _ in range(2)
        ])

    codes = sorted(x.status_code for x in responses)
    assert codes == [200, 409], [(x.status_code, x.text) for x in responses]
    assert executed.await_count == 1, "高危命令被并发执行了多次"

    r = await client.get(f"/intervention/requests?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == request_id)
    assert row["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# 2) 任务：并发 start 只有一个赢家
# ---------------------------------------------------------------------------
async def test_concurrent_task_start_only_dispatches_once(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"], "执行者")
    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "只应执行一次",
        "assigned_agent_id": agent["id"]}, headers=alice["headers"])
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    runner = AsyncMock()
    with patch("app.api.routes.tasks._run_in_background", runner):
        responses = await asyncio.gather(*[
            client.post(f"/tasks/{tid}/start", headers=alice["headers"])
            for _ in range(2)
        ])

    codes = sorted(x.status_code for x in responses)
    assert codes == [200, 409], [(x.status_code, x.text) for x in responses]
    assert runner.call_count == 1, "后台执行被重复提交"

    r = await client.get(f"/tasks/{tid}", headers=alice["headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"


# ---------------------------------------------------------------------------
# 3) 作战室：先 commit 会话行，再投递后台任务
# ---------------------------------------------------------------------------
async def test_war_room_kickoff_commits_before_background_submit(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    leader = await _make_agent(client, alice, co["id"], "负责人")

    db_path = engine.url.database
    observed: dict = {}

    def _spy_submit(coro_fn, *args, **kwargs):
        # 用**独立连接**读库：只有已提交的行才可见。若路由仍是"先 submit
        # 后 commit"，这里读不到会话行，后台 worker 同样读不到。
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            rows = conn.execute(
                "SELECT id, status FROM war_room_sessions").fetchall()
        finally:
            conn.close()
        observed["rows"] = rows

    with patch("app.background_jobs.submit_async",
               side_effect=_spy_submit) as submit:
        r = await client.post("/war-room/kickoff", json={
            "company_id": co["id"], "goal": "做一份市场分析",
            "leader_agent_id": leader["id"]}, headers=alice["headers"])

    assert r.status_code == 201, r.text
    assert submit.call_count == 1
    rows = observed.get("rows") or []
    assert len(rows) == 1, "投递后台时会话行尚未 commit（worker 将读不到）"
    assert rows[0][0] == r.json()["id"]
    assert rows[0][1] == "analyzing"


# ---------------------------------------------------------------------------
# 4) 工作流：取消后不再推进
# ---------------------------------------------------------------------------
async def test_workflow_cancel_stops_further_progress(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await client.post(
        f"/workflows?company_id={co['id']}",
        json={
            "name": "审批流",
            "nodes": [
                {"node_key": "s", "node_type": "START", "name": "开始"},
                {"node_key": "a", "node_type": "APPROVAL", "name": "审批"},
                {"node_key": "e", "node_type": "END", "name": "结束"},
            ],
            "edges": [
                {"from_node": "s", "to_node": "a"},
                {"from_node": "a", "to_node": "e"},
            ],
        },
        headers=alice["headers"])
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    # 后台投递是另一个 worker 的事；这里手动推进以便断言中间状态
    with patch("app.api.routes.workflows._run_in_background") as kick:
        r = await client.post(f"/workflows/{wid}/run",
                              headers=alice["headers"])
    assert r.status_code == 202, r.text
    eid = r.json()["execution_id"]
    assert kick.call_count == 1

    async with AsyncSessionLocal() as s:
        ex = await advance_execution(s, eid)
        status_after_first = ex.status
    assert status_after_first == RunStatus.WAITING_APPROVAL

    r = await client.post(f"/executions/{eid}/cancel",
                          params={"reason": "老板叫停"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text

    calls: list[str] = []
    original = WorkflowRuntime.execute_step

    async def _counting(self, node, ctx):
        calls.append(node.node_key)
        return await original(self, node, ctx)

    with patch.object(WorkflowRuntime, "execute_step", _counting):
        async with AsyncSessionLocal() as s:
            ex = await advance_execution(s, eid)
            final_status = ex.status

    assert final_status == RunStatus.CANCELLED, "已取消的实例被复活"
    assert calls == [], f"取消后仍执行了节点: {calls}"

    async with AsyncSessionLocal() as s:
        steps = (await s.execute(select(WorkflowStep).where(
            WorkflowStep.execution_id == eid))).scalars().all()
    assert [st.node_key for st in steps] == ["s", "a"], "取消后新开了节点"


# ---------------------------------------------------------------------------
# 5) 会议轮次租约：并发推进一轮只发言一次
# ---------------------------------------------------------------------------
async def test_concurrent_meeting_turn_speaks_once(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    a1 = await _make_agent(client, alice, co["id"], "甲")
    a2 = await _make_agent(client, alice, co["id"], "乙")
    r = await client.post("/meetings", json={
        "company_id": co["id"], "title": "并发会议", "topic": "同步进展",
        "participant_ids": [a1["id"], a2["id"]]}, headers=alice["headers"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]

    # 直接置为 RUNNING（不走 start 接口，避免真实 schedule_kick）
    async with AsyncSessionLocal() as s:
        m = await s.get(Meeting, mid)
        m.status = MeetingStatus.RUNNING
        await s.commit()

    async def _fake_chat(db, agent, system_prompt, user_prompt):
        # 拉长"发言"窗口，确保两个 worker 真的重叠
        await asyncio.sleep(0.05)
        return f"{agent.name} 的观点"

    async def _turn():
        async with AsyncSessionLocal() as s:
            from app.meeting_engine import run_meeting_turn
            return await run_meeting_turn(s, mid)

    with patch("app.meeting_engine._chat", _fake_chat), \
            patch("app.meeting_engine.schedule_next_round"):
        results = await asyncio.gather(_turn(), _turn())

    statuses = [x.get("status") for x in results]
    assert statuses.count("busy") == 1, results

    async with AsyncSessionLocal() as s:
        msgs = (await s.execute(select(MeetingMessage).where(
            MeetingMessage.meeting_id == mid,
            MeetingMessage.speaker_type == "agent"))).scalars().all()
    assert len(msgs) == 2, f"同一轮重复发言：{len(msgs)} 条"
