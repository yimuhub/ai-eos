"""证据链：聊天回复必须带得回来源（引用面板的数据底座）。

钉住四件事：
1. ``_extract_citations`` 只输出 ``{title, url, snippet}``，不把网页正文塞进 meta；
2. 无标题/无链接的条目丢弃、同 url 去重、最多 5 条、snippet 截断 200 字；
3. 非流式 ``/chat`` 响应与落库 meta 都是同一份 citations；
4. 流式 ``/chat/stream`` 的 done 帧与落库 meta 也是同一份 citations。
"""
from __future__ import annotations

import json

from app.api.routes import agent_chat
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


SEARCH_RESULT = {
    "query": "竞品价格",
    "results": [
        {"title": "竞品 A 官网", "href": "https://a.example.com/pricing",
         "body": "竞品 A 将基础版价格下调 20%，附赠三个月试用。"},
        {"title": "行业价格观察", "url": "https://news.example.com/p",
         "snippet": "多家厂商跟进降价。"},
    ],
}


# ---------------------------------------------------------------------------
# 1) 纯函数：提取与清洗
# ---------------------------------------------------------------------------
def test_extract_citations_normalizes_title_url_snippet():
    cites = agent_chat._extract_citations(SEARCH_RESULT)
    assert cites == [
        {"title": "竞品 A 官网", "url": "https://a.example.com/pricing",
         "snippet": "竞品 A 将基础版价格下调 20%，附赠三个月试用。"},
        {"title": "行业价格观察", "url": "https://news.example.com/p",
         "snippet": "多家厂商跟进降价。"},
    ]
    # 只保留三个字段，原始 body 等不落库
    assert set(cites[0]) == {"title", "url", "snippet"}


def test_extract_citations_falls_back_to_snippet_and_content():
    data = {"results": [
        {"title": "T1", "url": "https://x.test/1", "content": "正文摘要"},
        {"title": "T2", "url": "https://x.test/2", "snippet": "备用摘要"},
    ]}
    cites = agent_chat._extract_citations(data)
    assert [c["snippet"] for c in cites] == ["正文摘要", "备用摘要"]


def test_extract_citations_drops_items_without_url_or_title():
    data = {"results": [
        {"title": "没有链接", "body": "x"},
        {"url": "https://x.test/no-title", "body": "x"},
        {"title": "正常", "url": "https://x.test/ok", "body": "ok"},
        "不是对象",
    ]}
    cites = agent_chat._extract_citations(data)
    assert [c["url"] for c in cites] == ["https://x.test/ok"]


def test_extract_citations_dedupes_by_url():
    data = {"results": [
        {"title": "标题一", "url": "https://x.test/dup", "body": "a"},
        {"title": "标题二", "url": "https://x.test/dup", "body": "b"},
    ]}
    cites = agent_chat._extract_citations(data)
    assert len(cites) == 1
    assert cites[0]["title"] == "标题一"


def test_extract_citations_rejects_non_dict_and_missing_results():
    assert agent_chat._extract_citations(None) == []
    assert agent_chat._extract_citations("https://x.test") == []
    assert agent_chat._extract_citations({"results": "not-a-list"}) == []
    assert agent_chat._extract_citations({"error": "搜索失败"}) == []


def test_extract_citations_truncates_snippet_to_200_chars():
    data = {"results": [{"title": "长摘要", "url": "https://x.test/long",
                         "body": "字" * 500}]}
    cite = agent_chat._extract_citations(data)[0]
    assert len(cite["snippet"]) == agent_chat.CITATION_SNIPPET_CHARS == 200
    assert cite["snippet"] == "字" * 200


def test_extract_citations_caps_at_five():
    data = {"results": [
        {"title": f"T{i}", "url": f"https://x.test/{i}", "body": "b"}
        for i in range(9)]}
    cites = agent_chat._extract_citations(data)
    assert len(cites) == agent_chat.MAX_CITATIONS == 5
    assert [c["url"] for c in cites] == [f"https://x.test/{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# 2) 非流式：响应 + 落库 meta
# ---------------------------------------------------------------------------
async def test_non_stream_chat_returns_and_persists_citations(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    calls = {"n": 0}

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "usage": {}, "tool_calls": [{
                "id": "call_1",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "竞品价格"})},
            }]}
        return {"content": "根据搜索结果，竞品 A 降价了。", "usage": {},
                "tool_calls": None}

    async def fake_execute(db, fname, args, **kwargs):
        assert fname == "web_search"
        return {"result": SEARCH_RESULT}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)
    monkeypatch.setattr(agent_chat, "execute_skill_safe", fake_execute)

    r = await client.post(f"/agents/{agent['id']}/chat",
                          json={"message": "帮我查一下竞品最近的价格变化"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "根据搜索结果，竞品 A 降价了。"
    assert body["citations"], "响应里必须带 citations"
    assert body["citations"][0]["url"] == "https://a.example.com/pricing"
    assert set(body["citations"][0]) == {"title", "url", "snippet"}

    history = await _history(client, user, agent["id"])
    assert [m["role"] for m in history] == ["boss", "agent"]
    assert history[-1]["meta"]["citations"] == body["citations"], \
        "落库 meta 与响应必须是同一份来源"


# ---------------------------------------------------------------------------
# 3) 流式：done 帧 + 落库 meta
# ---------------------------------------------------------------------------
async def test_stream_chat_done_event_and_meta_carry_citations(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    calls = {"n": 0}

    async def fake_stream(self, model_ids, messages, tools=None, config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "done", "usage": {}, "model": "fake", "tool_calls": [{
                "id": "call_1",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "竞品价格"})},
            }]}
            return
        yield {"type": "delta", "text": "根据搜索结果，竞品 A 降价了。"}
        yield {"type": "done", "usage": {}, "model": "fake", "tool_calls": None}

    async def fake_execute(db, fname, args, **kwargs):
        assert fname == "web_search"
        return {"result": SEARCH_RESULT}

    monkeypatch.setattr(ModelGateway, "stream_with_agent_models", fake_stream)
    monkeypatch.setattr(agent_chat, "execute_skill_safe", fake_execute)

    r = await client.post(f"/agents/{agent['id']}/chat/stream",
                          json={"message": "帮我查一下竞品最近的价格变化"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["reply"] == "根据搜索结果，竞品 A 降价了。"
    assert done["citations"], "done 帧里必须带 citations"
    assert done["citations"][0]["url"] == "https://a.example.com/pricing"
    assert set(done["citations"][0]) == {"title", "url", "snippet"}

    history = await _history(client, user, agent["id"])
    assert [m["role"] for m in history] == ["boss", "agent"]
    assert history[-1]["meta"]["citations"] == done["citations"], \
        "流式落库 meta 与 done 帧必须是同一份来源"
