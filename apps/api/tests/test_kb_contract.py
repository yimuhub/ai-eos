"""知识库域（`/knowledge` + `/kb2`）的响应形状契约。

这里把「端点真实返回的键集合」和「`response_model` 声明的字段」两头钉死：
两头都必须等于测试里写清的那一组键。为什么敢用**相等**而不是"包含"：

* 只断言"响应里有这些键"→ 端点哪天多吐一个键测试照样绿，而前端永远不知道；
* 只断言模型字段 → 模型写漏一个字段时测试不会红，但序列化会**静默把它裁掉**，
  于是页面少一列且没有任何报错。

`_doc_out` / `retrieve()` 这些 helper 的键都在源码里，写死的键集就是它们的镜像；
helper 一改，这里立刻红——正是想要的效果。

顺带钉住两个真实缺陷的回归：

1. `POST /kb2/agents/{id}/documents` 过去**没有 return**，200 却回 `null`，
   前端只能整表重取；现在与公用库那条一样回 `{id, chunks}`。
2. `/kb2/copy` 与 `sync-from-global` 的 `count` 会小于请求的 `doc_ids` 长度
   （越权/不存在的源文档按"跳过"处理）——这是设计行为，但必须有键能让前端知道
   到底复制了几篇。
"""
from __future__ import annotations

import io
import uuid

from fastapi.routing import APIRoute, _IncludedRouter

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.schemas import responses_kb as kb_schemas

_KB2_MODULES = {"app.api.routes.kb2", "app.api.routes.knowledge"}


def _id() -> str:
    return str(uuid.uuid4())


def _keys(model) -> set[str]:
    return set(model.model_fields)


def _api_routes() -> list[APIRoute]:
    found: list[APIRoute] = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif isinstance(route, _IncludedRouter):
                walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                walk(route.routes)

    from app.main import app
    walk(app.routes)
    return found


async def _seed_agent(company_id: str, name: str = "知识库测试员工") -> str:
    agent_id = "kbagent_" + _id()[:8]
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=agent_id, company_id=company_id, name=name,
                    role="资料整理", status="IDLE"))
        await s.commit()
    return agent_id


# ---------------------------------------------------------------- 键集契约

DOC_ITEM_KEYS = {"id", "title", "source_type", "status", "chunk_count",
                 "project_id", "scope", "agent_id", "created_at"}
DOC_DETAIL_KEYS = {"id", "title", "content", "status", "chunk_count", "chunks"}
KB2_DOC_KEYS = {"id", "title", "scope", "category", "chunk_count", "summary",
                "has_original", "original_name", "skill_template", "created_at"}
HIT_KEYS = {"chunk_id", "document_id", "seq", "content", "score"}


async def test_knowledge_document_lifecycle_keys(client, tenants):
    """/knowledge 全链路：建 → 列表 → 详情 → 检索 → RAG → 删。"""
    co = tenants.alice_co["id"]
    body = {"title": "退货政策 v3", "content": "七天无理由退货。跨境商品需自行承担关税。",
            "source_type": "text"}
    r = await client.post(f"/knowledge/documents?company_id={co}",
                          json=body, headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    created = r.json()
    assert set(created) == _keys(kb_schemas.KnowledgeDocCreatedOut), created
    assert created["title"] == body["title"]
    assert created["status"] in {"PROCESSING", "READY", "FAILED"}, created
    assert created["chunk_count"] >= 1
    doc_id = created["id"]

    r = await client.get(f"/knowledge/documents?company_id={co}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list) and rows, rows
    row = next(d for d in rows if d["id"] == doc_id)
    assert set(row) == DOC_ITEM_KEYS, row
    assert set(rows[0]) == _keys(kb_schemas.KnowledgeDocItemOut)

    r = await client.get(f"/knowledge/documents/{doc_id}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    detail = r.json()
    assert set(detail) == DOC_DETAIL_KEYS, detail
    assert set(detail) == _keys(kb_schemas.KnowledgeDocDetailOut)
    assert detail["content"] == body["content"]
    assert all(set(c) == {"seq", "content"} for c in detail["chunks"]), detail
    assert all(set(c) == _keys(kb_schemas.KnowledgeChunkBriefOut)
               for c in detail["chunks"])

    r = await client.get(f"/knowledge/search?company_id={co}&q=退货",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    found = r.json()
    assert set(found) == _keys(kb_schemas.KnowledgeSearchOut), found
    assert found["query"] == "退货"
    assert found["results"], "关键词检索应命中刚入库的文档"
    assert all(set(h) == HIT_KEYS for h in found["results"]), found["results"]
    assert all(set(h) == _keys(kb_schemas.KnowledgeHitOut)
               for h in found["results"])

    r = await client.get(f"/knowledge/rag?company_id={co}&q=退货",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rag = r.json()
    assert set(rag) == _keys(kb_schemas.KnowledgeRagOut), rag
    assert isinstance(rag["context"], str)

    r = await client.delete(f"/knowledge/documents/{doc_id}",
                           headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(kb_schemas.KnowledgeDeletedOut), r.json()


async def test_knowledge_rejects_foreign_company(client, tenants):
    """`company_id` 由调用者给，必须校验——否则可枚举/读走别家知识库。"""
    r = await client.get(f"/knowledge/documents?company_id={tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- /kb2 私人库

async def test_kb2_create_private_doc_returns_id(client, tenants):
    """回归：这条端点曾漏了 return，200 却回 null。"""
    co = tenants.alice_co["id"]
    agent_id = await _seed_agent(co)
    r = await client.post(f"/kb2/agents/{agent_id}/documents",
                          json={"title": "话术库", "content": "开场：先确认身份。\n\n收尾：留联系方式。",
                                "category": "话术"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body is not None, "创建私人库文档必须回新文档 id，不能是 null"
    assert set(body) == _keys(kb_schemas.KBIndexedOut), body
    assert body["chunks"] >= 1

    r = await client.get(f"/kb2/agents/{agent_id}/documents",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list) and rows, rows
    assert set(rows[0]) == KB2_DOC_KEYS, rows[0]
    doc = next(d for d in rows if d["id"] == body["id"])
    assert doc["scope"] == "private"
    assert doc["category"] == "话术"
    assert doc["has_original"] is False
    assert isinstance(doc["summary"], str)


async def test_kb2_doc_list_is_empty_for_other_tenant(client, tenants):
    agent_id = await _seed_agent(tenants.bob_co["id"])
    r = await client.get(f"/kb2/agents/{agent_id}/documents",
                         headers=tenants.alice["headers"])
    # 越权读他人员工 → 404（不透露存在性）
    assert r.status_code == 404, r.text


async def test_kb2_edit_and_delete_document_shapes(client, tenants):
    co = tenants.alice_co["id"]
    r = await client.post(f"/kb2/global/documents?company_id={co}",
                          json={"title": "报销标准", "content": "差旅住宿上限 500 元/晚。"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    created = r.json()
    assert set(created) == _keys(kb_schemas.KBIndexedOut), created
    doc_id = created["id"]

    r = await client.patch(f"/kb2/documents/{doc_id}",
                           json={"title": "报销标准 v2",
                                 "content": "差旅住宿上限 600 元/晚。"},
                           headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}, r.text

    r = await client.get(f"/kb2/global/documents?company_id={co}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list) and rows, rows
    assert set(rows[0]) == KB2_DOC_KEYS, rows[0]
    assert next(d for d in rows if d["id"] == doc_id)["title"] == "报销标准 v2"

    r = await client.delete(f"/kb2/documents/{doc_id}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}, r.text


async def test_kb2_upload_branches(client, tenants):
    """两条分支的键集不同，且未走到的分支不得凭空多出 null 键。"""
    co = tenants.alice_co["id"]
    agent_id = await _seed_agent(co)

    r = await client.post(f"/kb2/agents/{agent_id}/upload",
                          files={"file": ("定价表.txt",
                                          io.BytesIO("标准版 99 元/月\n旗舰版 299 元/月".encode()),
                                          "text/plain")},
                          data={"category": "定价"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    ok = r.json()
    assert ok["converted"] is True, ok
    assert set(ok) == {"id", "converted", "chunks", "original_saved", "size"}, ok
    assert ok["chunks"] >= 1 and ok["size"] > 0

    # 解析不出文本 → 保留原始副本，如实标注（这条分支只有 note）
    r = await client.post(f"/kb2/agents/{agent_id}/upload",
                          files={"file": ("坏文件.pdf", io.BytesIO(b"%PDF-not-a-real-pdf"),
                                          "application/pdf")},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    bad = r.json()
    assert bad["converted"] is False, bad
    assert set(bad) == {"id", "converted", "note"}, bad

    r = await client.get(f"/kb2/agents/{agent_id}/documents",
                         headers=tenants.alice["headers"])
    rows = r.json()
    by_id = {d["id"]: d for d in rows}
    assert by_id[ok["id"]]["has_original"] is True
    assert by_id[ok["id"]]["original_name"] == "定价表.txt"
    assert by_id[bad["id"]]["has_original"] is True


async def test_kb2_upload_rejects_unlisted_extension(client, tenants):
    agent_id = await _seed_agent(tenants.alice_co["id"])
    r = await client.post(f"/kb2/agents/{agent_id}/upload",
                          files={"file": ("payload.sh", io.BytesIO(b"#!/bin/sh\nid"), "text/plain")},
                          headers=tenants.alice["headers"])
    assert r.status_code == 400, r.text


async def test_kb2_copy_and_sync(client, tenants):
    co = tenants.alice_co["id"]
    agent_id = await _seed_agent(co)
    r = await client.post(f"/kb2/global/documents?company_id={co}",
                          json={"title": "合规手册", "content": "个人信息不得外发。"},
                          headers=tenants.alice["headers"])
    src_id = r.json()["id"]

    r = await client.post("/kb2/copy",
                          json={"doc_ids": [src_id, "不存在的id"],
                                "target_agent_id": agent_id},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(kb_schemas.KBCopyOut), body
    assert body["count"] == 1, "不存在的源文档按跳过处理，count 只计真实复制的"
    assert set(body["copied"][0]) == _keys(kb_schemas.KBCopiedItemOut)
    assert body["copied"][0]["source"] == src_id

    r = await client.post(f"/kb2/agents/{agent_id}/sync-from-global",
                          json={"doc_ids": [src_id]},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(kb_schemas.KBCopyOut), r.text

    # 不能把别家公司的文档复制进自己的库
    bob_src = await client.post(
        f"/kb2/global/documents?company_id={tenants.bob_co['id']}",
        json={"title": "B 家机密", "content": "不应被读到。"},
        headers=tenants.bob["headers"])
    r = await client.post("/kb2/copy",
                          json={"doc_ids": [bob_src.json()["id"]],
                                "target_agent_id": agent_id},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0, r.text


async def test_kb2_templates_and_import(client, tenants):
    r = await client.get("/kb2/templates", headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    tpls = r.json()
    assert isinstance(tpls, list) and tpls, tpls
    assert set(tpls[0]) == _keys(kb_schemas.KBTemplateOut), tpls[0]
    key = tpls[0]["key"]
    assert tpls[0]["doc_count"] >= 1

    agent_id = await _seed_agent(tenants.alice_co["id"])
    r = await client.post(f"/kb2/agents/{agent_id}/import-template/{key}",
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(kb_schemas.KBImportOut), body
    assert body["count"] == len(body["imported"])
    assert set(body["imported"][0]) == _keys(kb_schemas.KBImportedItemOut)

    r = await client.get(f"/kb2/agents/{agent_id}/documents",
                         headers=tenants.alice["headers"])
    rows = r.json()
    assert all(d["skill_template"] == key for d in rows), rows
    assert all(set(d) == KB2_DOC_KEYS for d in rows), rows


async def test_kb2_import_unknown_template_is_404(client, tenants):
    agent_id = await _seed_agent(tenants.alice_co["id"])
    r = await client.post(f"/kb2/agents/{agent_id}/import-template/no_such_tpl",
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text


async def test_kb2_config_and_cost_profiles(client, tenants):
    agent_id = await _seed_agent(tenants.alice_co["id"])
    r = await client.put(f"/kb2/agents/{agent_id}/config",
                         json={"cost_mode": "economy", "kb_top_k": 4,
                               "kb_scope_mode": "private_first"},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(kb_schemas.KBAgentConfigOut), body
    assert body["memory_config"]["cost_mode"] == "economy"
    assert body["memory_config"]["kb_top_k"] == 4

    r = await client.put(f"/kb2/agents/{agent_id}/config",
                         json={"cost_mode": "不存在的档"},
                         headers=tenants.alice["headers"])
    assert r.status_code == 400, r.text

    r = await client.get("/kb2/cost-profiles", headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    profiles = r.json()
    assert set(profiles) >= {"precision", "balanced", "economy"}, profiles
    for profile in profiles.values():
        assert set(profile) == _keys(kb_schemas.KBCostProfileOut), profile


async def test_kb2_rag_preview_shape(client, tenants):
    co = tenants.alice_co["id"]
    agent_id = await _seed_agent(co)
    await client.post(f"/kb2/agents/{agent_id}/documents",
                      json={"title": "SLA 承诺",
                            "content": "线上故障 30 分钟内响应，2 小时内给出结论。"},
                      headers=tenants.alice["headers"])

    r = await client.get(f"/kb2/agents/{agent_id}/rag?q=故障响应",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(kb_schemas.KBRagPreviewOut), body
    assert isinstance(body["meta"], dict)
    assert body["hits"] >= 1, body

    # 同问第二次命中进程内缓存：from_cache 翻真，且 meta 换分支只留 profile/cached
    r2 = await client.get(f"/kb2/agents/{agent_id}/rag?q=故障响应",
                          headers=tenants.alice["headers"])
    assert r2.status_code == 200, r2.text
    cached = r2.json()
    assert cached["from_cache"] is True, cached
    assert set(cached) == _keys(kb_schemas.KBRagPreviewOut), cached

    # 回归：新建私人库文档必须让检索缓存失效——不失效的话这个问题会永远返回旧命中，
    # 刚入库的资料对员工来说等于不存在（create_global_doc 一直有这一步，
    # create_private_doc 曾漏掉）。
    r3 = await client.post(f"/kb2/agents/{agent_id}/documents",
                           json={"title": "复盘模板",
                                 "content": "故障复盘：时间线、根因、改进项、验证方式。"},
                           headers=tenants.alice["headers"])
    assert r3.status_code == 200, r3.text
    r4 = await client.get(f"/kb2/agents/{agent_id}/rag?q=故障响应",
                          headers=tenants.alice["headers"])
    assert r4.status_code == 200, r4.text
    assert r4.json()["from_cache"] is False, "新建文档后检索缓存应已失效"


# ---------------------------------------------------------------- 完整性护栏

def test_every_knowledge_endpoint_declares_a_response_model():
    """/knowledge 与 /kb2 的 JSON 端点必须全部带 response_model。

    这两个模块已全部收口（20/20），留这条是为了挡住"新端点照抄旧写法"。
    """
    missing: list[str] = []
    total = 0
    for route in _api_routes():
        if route.endpoint.__module__ not in _KB2_MODULES:
            continue
        total += 1
        if route.response_model is None:
            missing.append(f"{','.join(sorted(route.methods))} {route.path}")
    assert total >= 20, f"知识库端点数变了？实际 {total}"
    assert not missing, f"以下端点没有 response_model：{missing}"


def test_kb_models_allow_unknown_fields():
    """`extra="allow"` 是 A3 的硬约束：漏判字段时宁可多透出，不许静默裁剪。"""
    offenders = [name for name, obj in vars(kb_schemas).items()
                 if isinstance(obj, type)
                 and issubclass(obj, kb_schemas.BaseModel)
                 and obj.__module__ == kb_schemas.__name__
                 and obj.model_config.get("extra") != "allow"]
    assert not offenders, f"以下模型缺 extra='allow'：{offenders}"
