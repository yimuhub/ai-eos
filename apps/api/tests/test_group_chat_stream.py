"""群聊全过程可视化：POST /{scope}/{scope_id}/stream 事件契约与过程落库。

流式端点把每个 Agent 的"想/搜/调/答"全过程通过 SSE 发给老板，同时把
ProcessAccumulator.meta() 锁进 GroupChatMessage.content.process——刷新后
history 回放才能看到每个 Agent 的完整过程。断痕：
1. 事件序列（group_start / agent_start / agent_done / group_done / error）
2. 每个 agent 行的 process 落库（steps 非空、duration_ms 非空）
3. 空群与非法 scope 的边界
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.api.routes.group_chat import GroupChatMessage
from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway


def parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def _make_agents(client, user, company_id, n=2):
    out = []
    for i in range(n):
        r = await client.post("/agents", json={
            "company_id": company_id, "name": f"员工-{uuid.uuid4().hex[:4]}-{i}"},
            headers=user["headers"])
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


def _set_responses(monkeypatch, responses: list[dict]):
    """流水账假网关：每条回应拆成 delta + 最后一行 done，按调用顺序瓜分。"""

    async def fake_stream(self, agent, messages, tools=None, config=None):
        sent = list(messages)
        self.calls.append([{"role": m.get("role"),
                            "content": str(m.get("content") or "")}
                           for m in sent])
        if not self.responses:
            return
        # 先报模型名，再用分片 delta 吐正文
        yield {"type": "model", "model_id": "fake-model",
               "model_name": agent.name}
        body = self.responses.pop(0)
        text = body["content"] or ""
        for i in range(0, len(text), 3):
            yield {"type": "delta", "text": text[i:i + 3]}
        yield {"type": "done", "tool_calls": [],
               "model": agent.name, "used_model_id": "fake-model",
               "elapsed_ms": 12}

    async def fake_chat(self, agent, messages, tools=None, config=None):
        self.calls.append([{"role": m.get("role"),
                            "content": str(m.get("content") or "")}
                           for m in messages])
        return self.responses.pop(0) if self.responses \
            else {"content": "", "tool_calls": []}

    def _install(cls, attr, fn):
        fn.__self__ = None
        monkeypatch.setattr(cls, attr, fn)

    # 每一个被调用（按顺序）的 Agent 各分一段回应
    ModelGateway.responses = list(responses)

    _install(ModelGateway, "stream_with_agent_models_by_agent", fake_stream)
    _install(ModelGateway, "chat_with_agent_models_by_agent", fake_chat)


async def _group_with_agents(client, user, company_id, n=2):
    agents = await _make_agents(client, user, company_id, n)
    r = await client.post("/chat-groups", json={
        "company_id": company_id, "name": "流式两人群",
        "agent_ids": [a["id"] for a in agents]}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"], [a["name"] for a in agents]


async def test_stream_agents_run_in_parallel(client, tenants, monkeypatch):
    """并行发言：每人"思考"0.4s，2 人总耗时应明显低于串行的 0.8s。"""
    import asyncio
    import time

    async def fake_stream(self, agent, messages, tools=None, config=None):
        await asyncio.sleep(0.4)
        yield {"type": "model", "model_id": "fake-model",
               "model_name": agent.name}
        yield {"type": "delta", "text": f"{agent.name} 的意见"}
        yield {"type": "done", "tool_calls": [], "model": agent.name,
               "used_model_id": "fake-model", "elapsed_ms": 400}

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models_by_agent",
                        fake_stream)

    co_id = tenants.alice_co["id"]
    gid, names = await _group_with_agents(client, tenants.alice, co_id, 2)

    started = time.monotonic()
    r = await client.post(f"/group-chat/group/{gid}/stream?company_id={co_id}",
                          json={"message": "在吗"}, headers=tenants.alice["headers"])
    elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert elapsed < 0.7, f"两位员工串行跑完了（{elapsed:.2f}s），没有并行"

    events = parse_sse(r.text)
    done = [e for e in events if e["type"] == "agent_done"]
    assert len(done) == 2
    # 每条事件都能定位到自己的员工，前端才敢在并行时按 id 回填气泡
    assert {e["agent_name"] for e in done} == set(names)
    assert all(e.get("agent_id") for e in done)
    assert {e["text"] for e in done} == {f"{n} 的意见" for n in names}


async def test_stream_reports_events_and_process(client, tenants, monkeypatch):
    responses = [{"content": "我是员工-1。", "tool_calls": [], "finish_reason": "stop"},
                 {"content": "我是员工-2。", "tool_calls": [], "finish_reason": "stop"}]
    ModelGateway.calls = []
    _set_responses(monkeypatch, responses)

    co_id = tenants.alice_co["id"]
    gid, names = await _group_with_agents(client, tenants.alice, co_id, 2)

    r = await client.post(f"/group-chat/group/{gid}/stream?company_id={co_id}",
                          json={"message": "在吗"}, headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "group_start"
    assert kinds[-1] == "group_done"
    assert len([k for k in kinds if k == "agent_start"]) == 2
    assert len([k for k in kinds if k == "agent_done"]) == 2
    assert not [k for k in kinds if k == "error"], events

    # agent_done 带着正文和最终模型名。群聊已改为并行发言，事件到达顺序
    # 由调度决定——所以断言"谁说了什么"（集合 + 归属），不锁死先后。
    agent_done = [e for e in events if e["type"] == "agent_done"]
    texts = sorted(e["text"] for e in agent_done)
    assert texts == sorted(["我是员工-1。", "我是员工-2。"])
    models = sorted(e["model"] for e in agent_done)
    assert models == sorted(names)
    # 每条事件都能定位到自己的员工：并行时前端靠 id 回填正确的气泡
    assert {e["agent_id"] for e in agent_done} and \
        {e["agent_name"] for e in agent_done} == set(names)

    # history 回放的 process 从这条线产生
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(GroupChatMessage).where(
            GroupChatMessage.company_id == co_id,
            GroupChatMessage.scope == "group",
            GroupChatMessage.scope_id == gid))).scalars().all()
        agent_rows = [m for m in rows if m.role == "agent"]
        assert len(agent_rows) == 2
        for m in agent_rows:
            content = m.content or {}
            assert content["text"] in {r["content"] for r in responses}
            proc = content.get("process")
            assert proc, f"process 落库: {content}"
            assert proc["duration_ms"] > 0
            assert len(proc["steps"]) >= 2, proc["steps"]

    assert len(ModelGateway.calls) == 2, "每位 Agent 各调一次流"


async def test_stream_zero_agents(client, tenants):
    co_id = tenants.alice_co["id"]
    gid = uuid.uuid4().hex
    r = await client.post(f"/group-chat/group/{gid}/stream?company_id={co_id}",
                          json={"message": "在吗"}, headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "group_start"
    assert kinds[-1] == "group_done"
    assert not any(e["type"] == "agent_start" for e in events)


async def test_stream_rejects_bad_scope(client, tenants):
    r = await client.post("/group-chat/bogus/xyz/stream?company_id="
                          f"{tenants.alice_co['id']}", json={"message": "在吗"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 400, r.text


async def test_stream_empty_agent_penalty(client, tenants, monkeypatch):
    """空正文 Agent 也正常走完：重发一次后仍空，发 (未发言)。"""
    ModelGateway.calls = []
    _set_responses(monkeypatch, [
        {"content": "", "tool_calls": [], "finish_reason": "stop"},
        {"content": "", "tool_calls": [], "finish_reason": "stop"}])

    co_id = tenants.alice_co["id"]
    gid, names = await _group_with_agents(client, tenants.alice, co_id, 1)

    r = await client.post(f"/group-chat/group/{gid}/stream?company_id={co_id}",
                          json={"message": "在吗"}, headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    agent_done = [e for e in events if e["type"] == "agent_done"]
    assert len(agent_done) == 1
    assert agent_done[0]["text"] == "(未发言)"
    assert (e["text"] for e in events if e["type"] == "status") and \
        any("重发一次" in e["text"] for e in events if e["type"] == "status")
