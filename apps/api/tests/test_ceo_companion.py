"""CEO 常驻助手：入口契约、线程隔离、消息持久化、滚动摘要与省 token。

全部走临时 SQLite；模型调用用假网关，不产生真实费用。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.api.routes.agent_chat import (SUMMARY_KEEP_RECENT,
                                       SUMMARY_TRIGGER_MESSAGES,
                                       _maybe_summarize_thread,
                                       _model_ids_for_text, _skill_prompt)
from app.ceo_companion import ensure_default_thread
from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.agent import Agent
from app.models.agent_chat import AgentChatMessage
from app.models.ceo_thread import CeoThread
from app.models.memory import AgentMemory, MemoryType
from app.schemas.responses_ceo import (CeoCompanionOut, CeoThreadDeleteOut,
                                       CeoThreadListOut, CeoThreadMessagesOut,
                                       CeoThreadOut)

pytestmark = pytest.mark.asyncio


def _parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def _agent(client, user, company_id: str, name: str = "王总",
                 role: str = "总经理") -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": role},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _assign_ceo(client, user, company_id: str, agent_id: str) -> None:
    r = await client.post("/ceo/config", json={
        "company_id": company_id, "agent_id": agent_id},
        headers=user["headers"])
    assert r.status_code == 200, r.text


def _assert_keys(body: dict, model, where: str = "") -> None:
    assert set(body) == set(model.model_fields), (
        f"{where or model.__name__} 键集合不一致："
        f"多出 {sorted(set(body) - set(model.model_fields))}，"
        f"缺少 {sorted(set(model.model_fields) - set(body))}")


# ---------------------------------------------------------------------------
# 1) 入口契约
# ---------------------------------------------------------------------------

async def test_companion_without_ceo_is_empty_but_contract_complete(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await client.get(f"/ceo/companion?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_keys(body, CeoCompanionOut, "CeoCompanionOut")
    assert body["has_ceo"] is False
    assert body["thread_id"] is None
    assert body["quick_prompts"]


async def test_companion_after_assignment_has_thread_and_usage(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _agent(client, alice, co["id"])
    await _assign_ceo(client, alice, co["id"], agent["id"])

    r = await client.get(f"/ceo/companion?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_keys(body, CeoCompanionOut, "CeoCompanionOut")
    assert body["has_ceo"] is True
    assert body["ceo_agent_name"] == "CEO"
    assert body["thread_id"]
    assert body["today_tokens"] == 0
    assert body["budget_paused"] is False


# ---------------------------------------------------------------------------
# 2) 线程：创建 / 列表 / 跨租户 / 删除
# ---------------------------------------------------------------------------

async def test_thread_create_list_and_cross_tenant_guard(client, tenants):
    alice, bob = tenants.alice, tenants.bob
    co, bob_co = tenants.alice_co, tenants.bob_co
    agent = await _agent(client, alice, co["id"])
    await _assign_ceo(client, alice, co["id"], agent["id"])

    created = await client.post("/ceo/threads", json={
        "company_id": co["id"], "title": "季度复盘"},
        headers=alice["headers"])
    assert created.status_code == 201, created.text
    _assert_keys(created.json(), CeoThreadOut, "CeoThreadOut")
    tid = created.json()["id"]

    listed = await client.get(f"/ceo/threads?company_id={co['id']}",
                              headers=alice["headers"])
    assert listed.status_code == 200, listed.text
    body = listed.json()
    _assert_keys(body, CeoThreadListOut, "CeoThreadListOut")
    assert {t["id"] for t in body["threads"]} >= {tid}

    # 别的公司拿不到这条线程：跨租户一律 404，不泄露存在性
    bob_agent = await _agent(client, bob, bob_co["id"])
    await _assign_ceo(client, bob, bob_co["id"], bob_agent["id"])
    forbidden = await client.get(
        f"/ceo/threads/{tid}/messages?company_id={bob_co['id']}",
        headers=bob["headers"])
    assert forbidden.status_code == 404

    removed = await client.delete(
        f"/ceo/threads/{tid}?company_id={co['id']}",
        headers=alice["headers"])
    assert removed.status_code == 200, removed.text
    _assert_keys(removed.json(), CeoThreadDeleteOut, "CeoThreadDeleteOut")

    again = await client.get(
        f"/ceo/threads/{tid}/messages?company_id={co['id']}",
        headers=alice["headers"])
    assert again.status_code == 404


# ---------------------------------------------------------------------------
# 3) 消息：带 thread_id 的流式对话落到线程里
# ---------------------------------------------------------------------------

async def test_stream_chat_persists_into_thread(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _agent(client, alice, co["id"])
    await _assign_ceo(client, alice, co["id"], agent["id"])

    comp = await client.get(f"/ceo/companion?company_id={co['id']}",
                            headers=alice["headers"])
    tid = comp.json()["thread_id"]

    async def fake_stream(self, model_ids, messages, tools=None, config=None):
        for piece in ("收到，", "我先把风险列出来。"):
            yield {"type": "delta", "text": piece}
        yield {"type": "done", "usage": {}, "tool_calls": None, "model": "fake"}

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", fake_stream)

    sent = await client.post(
        f"/agents/{agent['id']}/chat/stream",
        json={"message": "今天有什么风险", "thread_id": tid},
        headers=alice["headers"])
    assert sent.status_code == 200, sent.text
    events = _parse_sse(sent.text)
    assert [e["text"] for e in events if e["type"] == "delta"] == [
        "收到，", "我先把风险列出来。"]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["thread_id"] == tid

    r = await client.get(
        f"/ceo/threads/{tid}/messages?company_id={co['id']}",
        headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_keys(body, CeoThreadMessagesOut, "CeoThreadMessagesOut")
    assert [m["role"] for m in body["messages"]] == ["boss", "agent"]
    assert body["messages"][1]["content"] == "收到，我先把风险列出来。"

    threads = await client.get(f"/ceo/threads?company_id={co['id']}",
                               headers=alice["headers"])
    row = [t for t in threads.json()["threads"] if t["id"] == tid][0]
    assert row["message_count"] == 2
    assert row["title"] == "今天有什么风险"


# ---------------------------------------------------------------------------
# 4) 分层记忆：滚动摘要 + 长期事实
# ---------------------------------------------------------------------------

class _FakeGateway:
    async def chat_with_agent_models(self, model_ids, messages, tools=None):
        assert any("滚动纪要" in m["content"] for m in messages), messages
        return {"content": "老板要求在周五前完成季度复盘。\n"
                           "MEM: 老板偏好每周一先看风险清单。"}


async def test_rolling_summary_updates_thread_and_writes_semantic_memory(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_row = await _agent(client, alice, co["id"])
    await _assign_ceo(client, alice, co["id"], agent_row["id"])

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_row["id"])
        thread = await ensure_default_thread(s, agent)
        for i in range(SUMMARY_TRIGGER_MESSAGES):
            s.add(AgentChatMessage(
                id=f"msg-{i}", agent_id=str(agent.id),
                company_id=str(agent.company_id), thread_id=str(thread.id),
                role="boss" if i % 2 == 0 else "agent",
                content=f"第 {i} 条对话内容", meta={}))
        thread.message_count = SUMMARY_TRIGGER_MESSAGES
        await s.flush()

        await _maybe_summarize_thread(s, agent, thread, _FakeGateway(),
                                      ["primary", "cheap"])
        await s.commit()

        refreshed = await s.get(CeoThread, str(thread.id))
        assert refreshed.summary == "老板要求在周五前完成季度复盘。"
        assert refreshed.summarized_count == (
            SUMMARY_TRIGGER_MESSAGES - SUMMARY_KEEP_RECENT)

        facts = (await s.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == str(agent.id),
                AgentMemory.memory_type == MemoryType.SEMANTIC))).scalars().all()
        assert [f.content for f in facts] == ["老板偏好每周一先看风险清单。"]
        assert all(f.expires_at is None for f in facts), "长期事实不得过期"


# ---------------------------------------------------------------------------
# 5) 省 token：技能只注入 top-2，短问题先走便宜模型
# ---------------------------------------------------------------------------

async def test_skill_prompt_keeps_index_but_limits_methodology(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_row = await _agent(client, alice, co["id"])
    await _assign_ceo(client, alice, co["id"], agent_row["id"])

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_row["id"])
        text = await _skill_prompt(s, agent, "帮我做一次风险与合规审查")

    assert "风险与合规审查" in text
    assert text.count("### ") <= 2, "方法论只能注入最相关的 2 张"
    # 索引仍覆盖全部绑定技能，模型知道自己会什么
    assert text.count("\n- ") >= 5


async def test_short_questions_route_to_cheap_model_first():
    assert _model_ids_for_text(["primary", "secondary", "fallback"], "在吗") == [
        "secondary", "fallback", "primary"]
    complex_text = "请帮我拆解下一季度的增长方案并评估风险"
    assert _model_ids_for_text(["primary", "secondary"], complex_text) == [
        "primary", "secondary"]
    assert _model_ids_for_text(["only"], "随便聊聊") == ["only"]
