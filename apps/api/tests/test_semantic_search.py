"""B5 本地语义重排：分词 / 同义词 / BM25 单测 + ``/search`` hybrid 契约。

这些用例全部离线：不读取模型 Key、不发网络请求，只验证纯 Python 重排与
租户隔离下的 API 行为。hybrid 的候选仍由 SQL 按 ``company_id`` 取出，
本文件同时钉死「同义词能召回」「keyword 模式不变」「每类≤5/总计≤20」。
"""
from __future__ import annotations

import pytest

from app.db.session import AsyncSessionLocal
from app.models.asset import Asset, AssetType
from app.services.semantic_search import expand_query, rank, tokenize

# pyproject 已开启 asyncio_mode=auto：异步用例无需逐个加 marker，
# 纯函数用例也不会被误标成 asyncio。


# ---------------------------------------------------------------------------
# 纯函数单测
# ---------------------------------------------------------------------------
def test_tokenize_mixes_ascii_cjk_and_drops_stopwords():
    assert tokenize("客户 PRD-2026 版本") == ["客户", "prd", "2026", "版本"]
    # CJK 连续串切成 2-gram；单字查询退化为单字
    assert tokenize("量子菠萝") == ["量子", "子菠", "菠萝"]
    assert tokenize("牛") == ["牛"]
    # 单字停用词被过滤，纯停用词查询得到空列表
    assert tokenize("的 了 是 在 和") == []
    assert tokenize("") == []
    assert tokenize(None) == []


def test_expand_query_weights_original_above_synonyms():
    weights = expand_query(tokenize("客户 PRD"))
    assert weights["客户"] == 1.0
    assert weights["prd"] == 1.0
    assert weights["用户"] == pytest.approx(0.6)
    assert weights["顾客"] == pytest.approx(0.6)
    assert weights["需求"] == pytest.approx(0.6)
    assert weights["功能"] == pytest.approx(0.6)


def test_rank_puts_relevant_first_and_filters_unrelated():
    docs = [
        ("季度总结", "本季度无重大事项"),
        ("客户回访", "与客户确认合同条款"),
    ]
    result = rank("客户", docs)
    assert [index for index, _score in result] == [1]
    assert result[0][1] > 0


def test_rank_recalls_synonyms():
    docs = [
        ("季度总结", "本季度无重大事项"),
        ("用户增长", "面向新用户投放广告"),
    ]
    result = rank("客户", docs)
    assert [index for index, _score in result] == [1], (
        "客户↔用户 的同义词扩展应召回没有字面命中的文档")


def test_rank_handles_empty_query_docs_and_blank_text():
    docs = [("客户", "正文"), ("", "")]
    assert rank("", docs) == []
    assert rank("的", docs) == []
    assert rank("客户", []) == []
    assert rank("客户", [("", ""), (None, None)]) == []


def test_rank_ties_keep_original_order():
    docs = [("客户", ""), ("客户", "")]
    result = rank("客户", docs)
    assert [index for index, _score in result] == [0, 1]


# ---------------------------------------------------------------------------
# API 夹具与播种工具
# ---------------------------------------------------------------------------
async def _create_agent(client, headers: dict, company_id: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": "语义检索助手"}, headers=headers)
    assert r.status_code == 201, f"建员工失败：{r.status_code} {r.text}"
    return r.json()["id"]


async def _create_task(client, headers: dict, company_id: str, title: str,
                       description: str | None = None) -> str:
    r = await client.post("/tasks", json={
        "company_id": company_id, "title": title,
        "description": description}, headers=headers)
    assert r.status_code == 201, f"建任务失败：{r.status_code} {r.text}"
    return r.json()["id"]


async def _create_memory(client, headers: dict, company_id: str,
                         agent_id: str, content: str) -> str:
    r = await client.post(
        "/memory/remember",
        params={"company_id": company_id},
        json={"agent_id": agent_id, "memory_type": "EPISODIC",
              "content": content},
        headers=headers,
    )
    assert r.status_code == 200, f"写记忆失败：{r.status_code} {r.text}"
    body = r.json()
    assert body.get("stored") is True, f"记忆未入库：{r.text}"
    return body["id"]


async def _create_document(client, headers: dict, company_id: str,
                           title: str, content: str) -> str:
    r = await client.post(
        "/knowledge/documents",
        params={"company_id": company_id},
        json={"title": title, "content": content},
        headers=headers,
    )
    assert r.status_code == 200, f"建文档失败：{r.status_code} {r.text}"
    return r.json()["id"]


async def _create_asset(company_id: str, name: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(Asset(company_id=company_id, name=name, type=AssetType.IMAGE,
                     path="artifacts/semantic-test.png", mime_type="image/png"))
        await db.commit()


def _groups(body: dict) -> dict[str, dict]:
    return {group["type"]: group for group in body["groups"]}


# ---------------------------------------------------------------------------
# API 行为
# ---------------------------------------------------------------------------
async def test_hybrid_recalls_synonyms_without_like_hit(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    agent_id = await _create_agent(client, alice["headers"], co)
    task_id = await _create_task(
        client, alice["headers"], co, "客户回访计划", "本周联系客户")
    memory_id = await _create_memory(
        client, alice["headers"], co, agent_id, "员工入职流程已经整理")
    doc_id = await _create_document(
        client, alice["headers"], co, "年度协议", "与供应商签署协议")

    # 用户→客户：候选里没有「用户」字面命中，靠同义词召回。
    r = await client.get("/search", params={
        "company_id": co, "q": "用户", "types": "task", "mode": "hybrid",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = _groups(r.json())["task"]["items"]
    assert [item["id"] for item in items] == [task_id]
    assert items[0]["score"] > 0

    # 招聘→员工/入职：记忆命中。
    r = await client.get("/search", params={
        "company_id": co, "q": "招聘", "types": "memory", "mode": "hybrid",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = _groups(r.json())["memory"]["items"]
    assert [item["id"] for item in items] == [memory_id]

    # 合同→协议：知识文档命中。
    r = await client.get("/search", params={
        "company_id": co, "q": "合同", "types": "knowledge", "mode": "hybrid",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = _groups(r.json())["knowledge"]["items"]
    assert [item["id"] for item in items] == [doc_id]


async def test_keyword_mode_is_unchanged(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    await _create_task(client, alice["headers"], co, "客户回访计划")

    # 字面命中：结构与旧契约完全一致，没有 score 字段。
    r = await client.get("/search", params={
        "company_id": co, "q": "客户", "mode": "keyword",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = _groups(r.json())["task"]["items"]
    assert len(items) == 1
    assert set(items[0]) == {"id", "title", "snippet", "href"}

    # keyword 不做同义词扩展：用户搜「用户」不会命中「客户」。
    r = await client.get("/search", params={
        "company_id": co, "q": "用户", "mode": "keyword",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["groups"] == []


async def test_hybrid_items_have_score_sorted_desc(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    for i in range(6):
        title = f"量子菠萝 方案{i}"
        description = "量子菠萝 " * (i + 1)
        await _create_task(client, alice["headers"], co, title, description)

    r = await client.get("/search", params={
        "company_id": co, "q": "量子菠萝", "types": "task", "mode": "hybrid",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = _groups(r.json())["task"]["items"]
    assert len(items) == 5, "hybrid 每类仍最多 5 条"
    assert all("score" in item for item in items)
    scores = [item["score"] for item in items]
    assert scores == sorted(scores, reverse=True), "items 必须按分数降序"
    assert all(score > 0 for score in scores)


async def test_hybrid_per_type_and_total_limits(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    agent_id = await _create_agent(client, alice["headers"], co)
    for i in range(6):
        await _create_task(client, alice["headers"], co,
                           f"量子菠萝 任务{i}", "量子菠萝 计划")
        await _create_memory(client, alice["headers"], co, agent_id,
                             f"量子菠萝 记忆{i}")
        await _create_document(client, alice["headers"], co,
                               f"量子菠萝 文档{i}", "量子菠萝 正文")
        await _create_asset(co, f"量子菠萝 素材{i}.png")

    r = await client.get("/search", params={
        "company_id": co, "q": "量子菠萝", "mode": "hybrid",
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [group["type"] for group in body["groups"]] == [
        "task", "memory", "knowledge", "asset"]
    assert all(len(group["items"]) <= 5 for group in body["groups"])
    assert sum(len(group["items"]) for group in body["groups"]) <= 20


@pytest.mark.parametrize("mode", ["vector", "", "HYBRIDX"])
async def test_unknown_mode_is_400(client, tenants, mode):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": "客户", "mode": mode,
    }, headers=tenants.alice["headers"])
    assert r.status_code == 400, f"mode={mode!r} 应 400：{r.status_code} {r.text}"


async def test_hybrid_cross_company_is_404(client, tenants):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": "客户", "mode": "hybrid",
    }, headers=tenants.bob["headers"])
    assert r.status_code == 404, f"跨租户 hybrid 检索必须 404：{r.text}"


async def test_hybrid_empty_result_is_200(client, tenants):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"],
        "q": "绝不存在的关键词zz", "mode": "hybrid",
    }, headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"query": "绝不存在的关键词zz", "groups": []}
