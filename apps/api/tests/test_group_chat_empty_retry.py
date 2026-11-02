"""群聊发言空正文：自动重试一次，被截断的还要抬预算，不再直接显示「(未发言)」。

上游把推理计入 completion tokens，所以"调用成功但正文为空"多是 max_tokens 被
思考吃满（finish_reason=length）——同一份上下文原样重发必然还是空。
也要钉住反面：模型只是要求调用工具时不能误判成"没话说"而重试——那会把
tool_calls 覆盖掉，员工从此搜索不出结果。
"""
from __future__ import annotations

import uuid

from app.api.routes import group_chat as gc
from app.api.routes.group_chat import _agent_speak
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent


class _FakeGw:
    """按脚本返回，记录每次调用收到的上下文与参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.configs: list[dict] = []

    async def chat_with_agent_models_by_agent(self, agent, messages,
                                              tools=None, config=None):
        self.calls.append([{"role": m.get("role"),
                            "content": str(m.get("content") or "")}
                           for m in messages])
        self.configs.append(dict(config or {}))
        if self.responses:
            return self.responses.pop(0)
        return {"content": "", "tool_calls": []}


async def _make_agent(company_id: str) -> Agent:
    aid = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=aid, company_id=company_id, name=f"员工-{aid[:5]}",
                    role="行业分析"))
        await s.commit()
    async with AsyncSessionLocal() as s:
        return await s.get(Agent, aid)


async def _quiet_rag(monkeypatch):
    """知识库检索与重试无关，替掉避免用例去打 embedding。"""
    async def _none(*_args, **_kwargs):
        return {"context_text": ""}
    monkeypatch.setattr("app.kb_engine.cost_optimized_rag", _none)


async def test_truncated_reasoning_retries_with_a_bigger_budget(
        client, tenants, monkeypatch):
    """思考占满 max_tokens → 正文 0 字。原样重发一定还是空，必须抬预算。

    实测上游把推理算进 completion tokens：Sage-A1.5 在 512 下
    finish_reason=length、正文空、思考 1900+ 字；同一份上下文放到 4096 就能出话。
    """
    await _quiet_rag(monkeypatch)
    agent = await _make_agent(tenants.alice_co["id"])
    gw = _FakeGw([{"content": "", "tool_calls": [], "finish_reason": "length"},
                  {"content": "我负责盯竞品和价格带，每周出一版对比结论。",
                   "tool_calls": [], "finish_reason": "stop"}])

    async with AsyncSessionLocal() as s:
        out = await _agent_speak(s, gw, agent, "你的职责是什么", [])

    assert len(gw.calls) == 2, "空正文应触发一次重发"
    assert gw.calls[0] == gw.calls[1], "重发用同一份上下文，不偷偷改指令"
    assert gw.configs[0]["max_tokens"] == 512
    assert gw.configs[1]["max_tokens"] == gc.GROUP_CHAT_RETRY_MAX_TOKENS, \
        "被截断的重试必须加大预算，否则只是白烧一次"
    assert "竞品" in out["text"]
    assert out["text"] != "(未发言)"


async def test_sampling_flake_retries_without_changing_the_budget(
        client, tenants, monkeypatch):
    await _quiet_rag(monkeypatch)
    agent = await _make_agent(tenants.alice_co["id"])
    gw = _FakeGw([{"content": "", "tool_calls": [], "finish_reason": "stop"},
                  {"content": "本周把投放素材过一遍。",
                   "tool_calls": [], "finish_reason": "stop"}])

    async with AsyncSessionLocal() as s:
        out = await _agent_speak(s, gw, agent, "你的职责是什么", [])

    assert gw.configs[0] == gw.configs[1], "没截断就别擅自加钱"
    assert "素材" in out["text"]


async def test_still_empty_after_retry_does_not_loop(client, tenants,
                                                     monkeypatch):
    await _quiet_rag(monkeypatch)
    agent = await _make_agent(tenants.alice_co["id"])
    gw = _FakeGw([{"content": "", "tool_calls": [], "finish_reason": "length"},
                  {"content": "", "tool_calls": [], "finish_reason": "length"}])

    async with AsyncSessionLocal() as s:
        out = await _agent_speak(s, gw, agent, "你的职责是什么", [])

    assert len(gw.calls) == 2, "只重试一次，不给一个哑掉的模型反复烧钱"
    assert out["text"] == "(未发言)"


async def test_tool_request_is_not_mistaken_for_an_empty_reply(
        client, tenants, monkeypatch):
    """空正文 + tool_calls 是"我要先搜索"，不是"没话说"。

    如果这里也重发，第二次返回会把 tool_calls 覆盖掉，搜索链路直接断。
    """
    await _quiet_rag(monkeypatch)

    async def _fake_search(_name, _args):
        return {"result": {"results": [{"title": "行情", "body": "环比涨 3%"}]}}
    monkeypatch.setattr("app.skill_system.execute_skill", _fake_search)

    agent = await _make_agent(tenants.alice_co["id"])
    gw = _FakeGw([
        {"content": "", "tool_calls": [
            {"function": {"name": "web_search",
                          "arguments": '{"query":"最新行情"}'}}]},
        {"content": "环比涨 3%，建议本周先跟进价格带。", "tool_calls": []},
    ])

    async with AsyncSessionLocal() as s:
        out = await _agent_speak(s, gw, agent, "最新行情如何", [])

    assert len(gw.calls) == 2
    assert "搜索结果" in gw.calls[1][-1]["content"], \
        "第二次调用是带搜索结果的总结轮，不是空正文重发"
    assert "价格带" in out["text"]
    assert out["search_used"] is True
