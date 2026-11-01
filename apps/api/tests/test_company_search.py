"""B5「公司大脑」：租户隔离的全局关键词检索。

覆盖四类真实资源（任务 / 记忆 / 知识文档 / 资产），并钉死三条安全边界：

* 他司数据不可见：同一关键词只返回本公司的行；
* 他司 company_id 一律 404，与「公司不存在」不可区分；
* LIKE 通配符被转义：``a%a`` / ``a_a`` 只按字面量搜索，不会放大成全表。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.session import AsyncSessionLocal
from app.models.asset import Asset, AssetType

KEYWORD = "量子菠萝"
BOB_SECRET = "BOBSECRET"


async def _create_agent(client, headers: dict, company_id: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": "检索助手"}, headers=headers)
    assert r.status_code == 201, f"建员工失败：{r.status_code} {r.text}"
    return r.json()["id"]


async def _create_task(client, headers: dict, company_id: str,
                       title: str, description: str | None = None) -> str:
    r = await client.post("/tasks", json={
        "company_id": company_id, "title": title,
        "description": description}, headers=headers)
    assert r.status_code == 201, f"建任务失败：{r.status_code} {r.text}"
    return r.json()["id"]


async def _create_memory(client, headers: dict, company_id: str,
                         agent_id: str, content: str) -> None:
    r = await client.post(
        "/memory/remember",
        params={"company_id": company_id},
        json={"agent_id": agent_id, "memory_type": "EPISODIC",
              "content": content},
        headers=headers,
    )
    assert r.status_code == 200, f"写记忆失败：{r.status_code} {r.text}"
    assert r.json().get("stored") is True, f"记忆未入库：{r.text}"


async def _create_document(client, headers: dict, company_id: str,
                           title: str, content: str) -> None:
    r = await client.post(
        "/knowledge/documents",
        params={"company_id": company_id},
        json={"title": title, "content": content},
        headers=headers,
    )
    assert r.status_code == 200, f"建文档失败：{r.status_code} {r.text}"


async def _create_asset(company_id: str, name: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(Asset(company_id=company_id, name=name, type=AssetType.IMAGE,
                     path="artifacts/search-test.png", mime_type="image/png"))
        await db.commit()


@pytest.fixture
async def seeded(client, tenants) -> SimpleNamespace:
    """Alice 公司四类资源各一条（都含 KEYWORD），Bob 公司一条同词任务。"""
    alice = tenants.alice
    alice_co = tenants.alice_co["id"]
    bob = tenants.bob
    bob_co = tenants.bob_co["id"]

    agent_id = await _create_agent(client, alice["headers"], alice_co)
    task_id = await _create_task(
        client, alice["headers"], alice_co,
        f"{KEYWORD} 任务", "围绕量子菠萝的项目计划")
    await _create_memory(client, alice["headers"], alice_co, agent_id,
                         f"记住：{KEYWORD} 的结论是可行")
    await _create_document(client, alice["headers"], alice_co,
                           f"{KEYWORD} 知识文档", "正文里也提到量子菠萝")
    await _create_asset(alice_co, f"{KEYWORD} 素材.png")
    bob_task_id = await _create_task(
        client, bob["headers"], bob_co, f"{BOB_SECRET} 他司任务")

    return SimpleNamespace(task_id=task_id, bob_task_id=bob_task_id)


async def test_search_hits_all_four_company_resource_types(
        client, tenants, seeded):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": f"  {KEYWORD}  ",
    }, headers=tenants.alice["headers"])
    assert r.status_code == 200, f"检索失败：{r.status_code} {r.text}"
    body = r.json()
    assert body["query"] == KEYWORD, "响应 query 应回显去除空白后的关键词"
    assert [g["type"] for g in body["groups"]] == [
        "task", "memory", "knowledge", "asset"], (
        f"分组顺序不稳定或类型缺失：{body['groups']}")
    for group in body["groups"]:
        assert group["label"], f"{group['type']} 缺少展示名"
        assert 1 <= len(group["items"]) <= 5, f"{group['type']} 条数异常"
        for item in group["items"]:
            # 默认 mode=hybrid：在旧契约四个字段之外显式携带本地重排分数。
            assert set(item) == {"id", "title", "snippet", "href", "score"}
            assert item["score"] > 0
            assert item["id"] and item["title"]
            assert item["href"] in {"/tasks", "/memory", "/knowledge", "/assets"}
    task_group = body["groups"][0]
    assert task_group["items"][0]["id"] == seeded.task_id


async def test_search_empty_result_returns_empty_groups(client, tenants):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": "绝不存在的关键词zz",
    }, headers=tenants.alice["headers"])
    assert r.status_code == 200, f"空结果不应是错误：{r.status_code} {r.text}"
    assert r.json() == {"query": "绝不存在的关键词zz", "groups": []}


@pytest.mark.parametrize("bad_q", ["a", " a ", "  ", "x" * 101])
async def test_search_rejects_short_or_oversized_query(client, tenants, bad_q):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": bad_q,
    }, headers=tenants.alice["headers"])
    assert r.status_code == 400, (
        f"q={bad_q!r} 应被拒绝，实际 {r.status_code} {r.text}")


async def test_search_cross_tenant_is_404_and_indistinguishable(
        client, tenants, seeded):
    # 他司 company_id：404，且不能带出 Bob 的任务
    r = await client.get("/search", params={
        "company_id": tenants.bob_co["id"], "q": BOB_SECRET,
    }, headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户应 404：{r.status_code} {r.text}"
    assert BOB_SECRET not in r.text

    # 在自家公司里搜 Bob 的标记：查不到
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": BOB_SECRET,
    }, headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert r.json()["groups"] == [], f"租户隔离失效，返回了 Bob 的数据：{r.text}"

    # 不存在的公司：与「他司」完全同一个 404
    r = await client.get("/search", params={
        "company_id": "00000000-0000-0000-0000-000000000000", "q": KEYWORD,
    }, headers=tenants.alice["headers"])
    assert r.status_code == 404
    assert r.json().get("detail") == "Company not found"


async def test_search_like_wildcards_are_literal(client, tenants):
    co = tenants.alice_co["id"]
    await _create_task(client, tenants.alice["headers"], co, "alpha one")
    await _create_task(client, tenants.alice["headers"], co, "beta_two")

    for wildcard in ("a%a", "a_a"):
        r = await client.get("/search", params={
            "company_id": co, "q": wildcard,
        }, headers=tenants.alice["headers"])
        assert r.status_code == 200, f"{wildcard} 检索失败：{r.text}"
        assert r.json()["groups"] == [], (
            f"LIKE 通配符未转义：{wildcard!r} 命中了 {r.json()['groups']}")


async def test_search_types_filter_keeps_requested_groups_only(
        client, tenants, seeded):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": KEYWORD,
        "types": "asset,task",
    }, headers=tenants.alice["headers"])
    assert r.status_code == 200, f"types 过滤失败：{r.text}"
    assert [g["type"] for g in r.json()["groups"]] == ["task", "asset"]

    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": KEYWORD,
        "types": "task,unknown",
    }, headers=tenants.alice["headers"])
    assert r.status_code == 400, f"未知类型应 400：{r.status_code} {r.text}"


async def test_search_requires_authentication(client, tenants):
    r = await client.get("/search", params={
        "company_id": tenants.alice_co["id"], "q": KEYWORD})
    assert r.status_code == 401, f"未登录应 401：{r.status_code} {r.text}"
