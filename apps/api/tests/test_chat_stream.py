"""聊天流式输出（SSE）。

钉住四件容易退化的事：
1. 文档标记 ``[GENERATE_DOC]`` 是逐字到达的，绝不能漏进气泡；
2. 流式回复必须落库，刷新后历史里能看到同样一段话；
3. 模型挂掉时要给前端一个 ``error`` 帧，老板那句话仍在历史里；
4. 不需要 LLM 的短路径（安排任务）同样走流式帧，前端只维护一套逻辑。

另外覆盖一个真实缺陷的回归：``/chat`` 在"文档生成没出错"时会因为
``doc_error`` 未初始化抛 NameError，表现是回复已落库、接口却 500。
"""
from __future__ import annotations

import json

from app.api.routes.agent_chat import DOC_OPEN, DocMarkerFilter, visible_reply
from app.model_gateway import ModelGateway


def parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def _make_agent(client, user, company) -> dict:
    r = await client.post("/agents", json={
        "company_id": company["id"], "name": "小红", "role": "市场分析"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _history(client, user, aid) -> list[dict]:
    r = await client.get(f"/agents/{aid}/chat/history",
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1) 纯函数：标记过滤器
# ---------------------------------------------------------------------------
def test_doc_marker_never_leaks_partial_fragments():
    """把标记拆成单字符下发，任何一个字都不能提前放行。"""
    flt = DocMarkerFilter()
    payload = f"价格表已整理好。{DOC_OPEN}{{\"type\":\"md\",\"title\":\"t\"}}[/GENERATE_DOC]"
    streamed = "".join(flt.feed(ch) for ch in payload) + flt.flush()

    assert "GENERATE_DOC" not in streamed
    assert streamed.strip() == "价格表已整理好。"
    # 原文仍完整保留，供文档生成使用
    assert payload == flt.raw


def test_doc_marker_filter_flushes_unclosed_text():
    """普通正文（结尾恰好像标记前缀）在流结束时必须补发，不能吞字。"""
    flt = DocMarkerFilter()
    assert flt.feed("结论是 [") == "结论是 "
    assert flt.flush() == "["
    assert visible_reply("结论是 [") == "结论是 ["


def test_visible_reply_strips_marker_block():
    raw = '好的。\n[GENERATE_DOC]\n{"type":"md","title":"周报","content":"x"}\n[/GENERATE_DOC]'
    assert visible_reply(raw) == "好的。"


# ---------------------------------------------------------------------------
# 2) 端到端：流式帧 + 落库
# ---------------------------------------------------------------------------
async def test_stream_chat_emits_deltas_and_persists(client, make_user,
                                                     make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    async def fake_stream(self, model_ids, messages, tools=None, config=None):
        for piece in ("我在，", "老板。", "先把竞品定价核一遍。"):
            yield {"type": "delta", "text": piece}
        yield {"type": "done", "usage": {}, "tool_calls": None, "model": "fake"}

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", fake_stream)

    r = await client.post(f"/agents/{agent['id']}/chat/stream",
                          json={"message": "今天先做什么"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(r.text)
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["我在，", "老板。", "先把竞品定价核一遍。"]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["reply"] == "我在，老板。先把竞品定价核一遍。"

    history = await _history(client, user, agent["id"])
    assert [m["role"] for m in history] == ["boss", "agent"]
    assert history[1]["content"] == done["reply"], "流式回复没有落库"


async def test_stream_chat_strips_doc_marker_from_stream(client, make_user,
                                                         make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)
    marker = (f'{DOC_OPEN}\n{{"type":"md","title":"周报",'
              '"content":"# 本周"}\n[/GENERATE_DOC]')

    async def fake_stream(self, model_ids, messages, tools=None, config=None):
        for piece in ("周报已经写好。", marker[:10], marker[10:]):
            yield {"type": "delta", "text": piece}
        yield {"type": "done", "usage": {}, "tool_calls": None, "model": "fake"}

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", fake_stream)

    r = await client.post(f"/agents/{agent['id']}/chat/stream",
                          json={"message": "写个周报"},
                          headers=user["headers"])
    events = parse_sse(r.text)
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "GENERATE_DOC" not in streamed
    assert streamed.strip() == "周报已经写好。"

    done = [e for e in events if e["type"] == "done"][0]
    assert done["reply"] == "周报已经写好。"
    assert done["file"] or done["doc_error"], "标记被吃掉了却没有产出说明"


# ---------------------------------------------------------------------------
# 3) 失败路径：error 帧 + 老板那句话仍保留
# ---------------------------------------------------------------------------
async def test_stream_chat_error_frame_keeps_boss_message(client, make_user,
                                                          make_company,
                                                          monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    async def boom(self, model_ids, messages, tools=None, config=None):
        raise RuntimeError("该员工尚未绑定任何模型，无法执行任务。")
        yield  # pragma: no cover — 让它保持异步生成器语义

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", boom)

    r = await client.post(f"/agents/{agent['id']}/chat/stream",
                          json={"message": "帮我看看数据"},
                          headers=user["headers"])
    events = parse_sse(r.text)
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "尚未绑定任何模型" in errors[0]["message"]

    history = await _history(client, user, agent["id"])
    assert [m["role"] for m in history] == ["boss"], "失败时不应写入空的 agent 回复"
    assert history[0]["content"] == "帮我看看数据"


# ---------------------------------------------------------------------------
# 4) 短路径：安排任务不需要 LLM，也走流式帧
# ---------------------------------------------------------------------------
async def test_stream_chat_create_task_short_path(client, make_user,
                                                  make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    async def must_not_call(self, *a, **kw):
        raise AssertionError("安排任务不该调用 LLM")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", must_not_call)

    r = await client.post(f"/agents/{agent['id']}/chat/stream",
                          json={"message": "安排任务：调研三家竞品的定价策略"},
                          headers=user["headers"])
    events = parse_sse(r.text)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["actions"][0]["type"] == "create_task"
    assert "已收到工作安排" in done["reply"]

    tasks = await client.get(f"/tasks?company_id={company['id']}",
                             headers=user["headers"])
    assert tasks.status_code == 200
    assert any(t["id"] == done["actions"][0]["task_id"] for t in tasks.json())


# ---------------------------------------------------------------------------
# 5) 回归：/chat 在无文档标记时不能 500
# ---------------------------------------------------------------------------
async def test_non_stream_chat_returns_200_without_doc_marker(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        return {"content": "好的，我记下了。", "usage": {}, "tool_calls": None}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)

    r = await client.post(f"/agents/{agent['id']}/chat",
                          json={"message": "记一下：周五交周报"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "好的，我记下了。"
    assert body["file"] is None and body["doc_error"] is None


async def test_stream_chat_requires_ownership(client, tenants):
    """跨租户：别人的员工不允许被流式对话。"""
    r = await client.post("/agents", json={
        "company_id": tenants.bob_co["id"], "name": "Bob 的员工"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    other = r.json()

    r = await client.post(f"/agents/{other['id']}/chat/stream",
                          json={"message": "hi"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 404
