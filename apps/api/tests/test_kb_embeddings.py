"""V6 M1-f 知识库混合检索（向量语义 + 关键词）回归。

全程离线：embedding 用确定性伪实现（8 维、主题族共享基方向 + 字符 hash
落点）monkeypatch 掉 ``app.kb_embedding.embed_texts``，验证三件事：

1. 有向量时「用词完全不同」的语义相近块能被检索上来——这正是向量路径
   存在的意义，关键词 bigram 永远做不到；
2. 无配置/调用失败/维度不一致时，行为与旧版纯关键词检索一致；
3. embed 环节出任何岔子都不阻塞入库。

另用假 client 验证 embed_texts 对 OpenAI 兼容 /embeddings 的请求形状
（URL 拼法、Bearer 头、model/input 体）与响应解析、异常降级。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal

FAKE_DIM = 8

# 主题基向量：同族词共享一个基方向，模拟「语义相近的文本向量相近」。
# 各主题方向互相正交，cos 只有 0/1 两档，断言好写也稳。
_TOPIC_BASES = {
    "退款": (1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "物流": (0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0),
    "发票": (0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0),
}
_TOPIC_SYNONYMS = {
    "退款": ("退货", "退钱", "返款"),
    "物流": ("发货", "快递", "配送"),
    "发票": ("收据", "票据", "开票"),
}


def _fake_vec(text: str) -> list[float]:
    """确定性伪嵌入：命中主题词取主题方向，否则按字符 hash 稳定落点。"""
    vec = [0.0] * FAKE_DIM
    for topic, base in _TOPIC_BASES.items():
        if any(w in text for w in (topic, *_TOPIC_SYNONYMS[topic])):
            for i, v in enumerate(base):
                vec[i] += v
    if not any(vec):
        # md5 而非内置 hash()：后者按进程随机化，跨进程断言不可复现
        for ch in text:
            digest = hashlib.md5(ch.encode("utf-8")).digest()
            vec[int.from_bytes(digest[:2], "big") % FAKE_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


async def _fake_embed_texts(db, company_id, texts):
    return [_fake_vec(t) for t in texts]


async def _none_embed(db, company_id, texts):
    return None


@pytest.fixture
def fake_embed(monkeypatch):
    from app import kb_embedding
    monkeypatch.setattr(kb_embedding, "embed_texts", _fake_embed_texts)


async def _ingest(company_id: str, title: str, content: str) -> str:
    """走真实 ingest_document 入库（连同 embed 路径一起被测）。"""
    from app.knowledge_engine import ingest_document
    from app.models.knowledge import KBDocument, KBDocumentStatus

    async with AsyncSessionLocal() as s:
        doc = KBDocument(id=str(uuid.uuid4()), company_id=company_id,
                         title=title, content=content, source_type="text",
                         status=KBDocumentStatus.PROCESSING)
        s.add(doc)
        await s.flush()
        chunks = await ingest_document(s, doc)
        assert chunks >= 1
        return doc.id


async def _install_model(company_id: str | None, base_url: str,
                         api_key_env: str | None = None) -> None:
    from app.models.agent import Model, ModelProviderType

    async with AsyncSessionLocal() as s:
        s.add(Model(company_id=company_id, name="嵌入模型",
                    provider_type=ModelProviderType.OPENAI_COMPAT,
                    base_url=base_url, model_id="emb-test-1",
                    model_type="embedding", enabled=True,
                    api_key_env=api_key_env))
        await s.commit()


# ------------------------------------------------------- 向量的存在意义

async def test_vector_hits_when_keywords_cannot(fake_embed, tenants,
                                                monkeypatch):
    """查询与块零关键词重合，纯语义照样命中；关掉向量则什么都查不到。"""
    from app import kb_embedding
    from app.knowledge_engine import retrieve

    co = tenants.alice_co["id"]
    await _ingest(co, "售后政策",
                  "退货政策：签收后七日内可无理由退货，影响二次销售的商品除外。")
    await _ingest(co, "物流说明",
                  "发货时效：现货下单后四十八小时内出库，偏远地区顺延。")

    # 「返款」与块里的「退货」零 bigram 重合——关键词路径永远到不了这里
    query = "返款流程怎么走"
    async with AsyncSessionLocal() as s:
        hits = await retrieve(s, co, query, top_k=5)
    assert hits, "关键词对不上时向量必须兜住"
    assert "退货政策" in hits[0]["content"]
    assert hits[0]["score"] == pytest.approx(0.6, abs=1e-3), hits[0]

    # 对照组：同样的库、同样的查询，向量不可用时退回关键词 → 空
    monkeypatch.setattr(kb_embedding, "embed_texts", _none_embed)
    async with AsyncSessionLocal() as s:
        assert await retrieve(s, co, query, top_k=5) == [], \
            "零关键词重合的查询在纯关键词路径下必须为空"


async def test_hybrid_formula_and_weights(fake_embed, tenants):
    """score = 0.6 * cosine01 + 0.4 * keyword，逐位复算。"""
    from app import kb_embedding
    from app.knowledge_engine import (KEYWORD_WEIGHT, VECTOR_WEIGHT,
                                      _tokens, retrieve)
    from app.models.knowledge import KBChunk

    # 定版权重——要调权重必须连这里一起改，防止悄悄漂移
    assert VECTOR_WEIGHT == 0.6 and KEYWORD_WEIGHT == 0.4

    co = tenants.alice_co["id"]
    content = "退货政策：七日无理由退货。"
    await _ingest(co, "售后", content)
    async with AsyncSessionLocal() as s:
        chunk = (await s.execute(
            select(KBChunk).where(KBChunk.company_id == co))).scalars().one()
        hits = await retrieve(s, co, "退货流程", top_k=3)

    assert hits and hits[0]["chunk_id"] == chunk.id
    q_tokens = _tokens("退货流程")
    r_tokens = set(chunk.keywords or [])
    overlap = len(q_tokens & r_tokens)
    assert overlap > 0, "测试前提：该查询与块有关键词重合"
    kw = overlap / (len(q_tokens) ** 0.5 * len(r_tokens) ** 0.5)
    expected = round(VECTOR_WEIGHT
                     * kb_embedding.cosine_similarity(_fake_vec("退货流程"),
                                                      _fake_vec(content))
                     + KEYWORD_WEIGHT * kw, 4)
    assert hits[0]["score"] == pytest.approx(expected, abs=1e-9), hits[0]


# ------------------------------------------------------- 降级路径

async def test_no_config_keeps_keyword_only(tenants):
    """无 embedding 配置：embed_texts 返回 None，检索与旧版完全一致。"""
    from app import kb_embedding
    from app.knowledge_engine import retrieve
    from app.models.knowledge import KBChunk

    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as s:
        assert await kb_embedding.embed_texts(s, co, ["测试"]) is None

    doc_id = await _ingest(co, "报销", "差旅住宿上限 500 元每晚。")
    async with AsyncSessionLocal() as s:
        chunk = (await s.execute(
            select(KBChunk).where(KBChunk.document_id == doc_id)
        )).scalars().one()
        assert chunk.embedding is None, "无配置时不得写向量"

        hits = await retrieve(s, co, "住宿 报销", top_k=5)
        assert hits and hits[0]["chunk_id"] == chunk.id
        assert set(hits[0]) == {"chunk_id", "document_id", "seq",
                                "content", "score"}, hits[0]
        # 关键词零重合的查询：旧版行为是空，无向量时必须原样保持
        assert await retrieve(s, co, "完全无关的查询词组", top_k=5) == []


async def test_dimension_mismatch_falls_back_to_keyword(fake_embed, tenants,
                                                        monkeypatch):
    """查询向量维度与库中向量不一致（模型被换过）→ 整体退回关键词。"""
    from app import kb_embedding
    from app.knowledge_engine import _tokens, retrieve
    from app.models.knowledge import KBChunk

    co = tenants.alice_co["id"]
    content = "退货政策：七日无理由退货。"
    doc_id = await _ingest(co, "售后", content)  # 8 维入库
    async with AsyncSessionLocal() as s:
        chunk = (await s.execute(
            select(KBChunk).where(KBChunk.document_id == doc_id)
        )).scalars().one()
        assert chunk.embedding is not None

    async def _narrow_embed(db, company_id, texts):
        return [_fake_vec(t)[:6] for t in texts]  # 查询侧变成 6 维

    monkeypatch.setattr(kb_embedding, "embed_texts", _narrow_embed)
    async with AsyncSessionLocal() as s:
        # 零关键词重合：维度不匹配判死向量路径，不得凭「伪语义」入围
        assert await retrieve(s, co, "返款流程怎么走", top_k=5) == []
        # 有关键词重合：分数必须是纯关键词分（没有被 0.4 缩水）
        hits = await retrieve(s, co, "退货", top_k=5)
    r_tokens = set(chunk.keywords or [])
    kw = len(_tokens("退货") & r_tokens) / (len(_tokens("退货")) ** 0.5
                                            * len(r_tokens) ** 0.5)
    assert hits and hits[0]["chunk_id"] == chunk.id
    assert hits[0]["score"] == pytest.approx(round(kw, 4), abs=1e-9)


async def test_corrupted_vector_falls_back_to_keyword(tenants, fake_embed):
    """长度对但元素是脏数据的手改向量：退回关键词分，绝不 500。"""
    from app import kb_embedding
    from app.knowledge_engine import _tokens, retrieve
    from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus

    co = tenants.alice_co["id"]
    content = "退货政策：七日无理由退货。"
    async with AsyncSessionLocal() as s:
        doc = KBDocument(id=str(uuid.uuid4()), company_id=co, title="脏",
                         content=content, source_type="text",
                         status=KBDocumentStatus.READY)
        s.add(doc)
        await s.flush()
        s.add(KBChunk(id=str(uuid.uuid4()), document_id=doc.id,
                      company_id=co, seq=0, content=content,
                      keywords=["退货"],
                      embedding=["脏"] * FAKE_DIM))  # 8 个字符串：长度对、类型错
        await s.commit()

    async with AsyncSessionLocal() as s:
        # 有关键词重合：脏向量按「无向量」处理，分数是纯关键词分
        # （检索打分用的分词集合是存储的 keywords 列，不是现场重切 content）
        chunk = (await s.execute(
            select(KBChunk).where(KBChunk.company_id == co))).scalars().one()
        hits = await retrieve(s, co, "退货流程", top_k=3)
        r_tokens = set(chunk.keywords or [])
        kw = len(_tokens("退货流程") & r_tokens) / (
            len(_tokens("退货流程")) ** 0.5 * len(r_tokens) ** 0.5)
        assert hits and hits[0]["score"] == pytest.approx(round(kw, 4), abs=1e-9)
        # 零关键词重合 + 脏向量：既进不了语义通道也不崩，返回空
        assert await retrieve(s, co, "返款流程怎么走", top_k=3) == []
    assert kb_embedding.usable_vector(["脏"] * FAKE_DIM, _fake_vec("x")) is None


async def test_ingest_survives_embed_exception(tenants, monkeypatch):
    """embed 抛异常绝不阻塞入库：文档照常 READY，块无向量。"""
    from app import kb_embedding
    from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus

    co = tenants.alice_co["id"]

    async def _boom(db, company_id, texts):
        raise RuntimeError("embedding 服务挂了")

    monkeypatch.setattr(kb_embedding, "embed_texts", _boom)
    # 两个超长段落 → 强制多 chunk（短内容会被滑窗合并成 1 块）
    doc_id = await _ingest(co, "坏天气", "甲" * 600 + "\n\n" + "乙" * 600)
    async with AsyncSessionLocal() as s:
        doc = await s.get(KBDocument, doc_id)
        assert doc.status == KBDocumentStatus.READY
        assert doc.chunk_count >= 2
        chunks = (await s.execute(
            select(KBChunk).where(KBChunk.document_id == doc_id)
            .order_by(KBChunk.seq.asc()))).scalars().all()
        assert len(chunks) == doc.chunk_count
        assert all(c.embedding is None for c in chunks)

    # 返回 None（「这批没有向量」）同样不影响入库
    monkeypatch.setattr(kb_embedding, "embed_texts", _none_embed)
    doc_id2 = await _ingest(co, "好天气", "恢复正常营业。")
    async with AsyncSessionLocal() as s:
        doc2 = await s.get(KBDocument, doc_id2)
        assert doc2.status == KBDocumentStatus.READY


# ------------------------------------------------------- 候选池

async def test_vector_path_extends_candidate_pool_to_500(tenants, fake_embed,
                                                         monkeypatch):
    """260 块新噪声 + 1 块旧命中：向量路径把候选池提到 500 才能捞到它。"""
    from app import kb_embedding
    from app.knowledge_engine import retrieve
    from app.models.knowledge import KBChunk, KBDocument, KBDocumentStatus

    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as s:
        doc = KBDocument(id=str(uuid.uuid4()), company_id=co, title="池",
                         content="x", source_type="text",
                         status=KBDocumentStatus.READY)
        s.add(doc)
        await s.flush()
        base = datetime(2026, 1, 1)
        for i in range(260):
            s.add(KBChunk(id=str(uuid.uuid4()), document_id=doc.id,
                          company_id=co, seq=i, content="常规条款说明",
                          keywords=["常规"], embedding=None,
                          created_at=base + timedelta(seconds=i)))
        content = "退货政策：七日无理由退货。"
        target = KBChunk(id=str(uuid.uuid4()), document_id=doc.id,
                         company_id=co, seq=999, content=content,
                         keywords=["退货"], embedding=_fake_vec(content),
                         created_at=datetime(2025, 12, 1))
        target_id = target.id
        s.add(target)
        await s.commit()

    query = "返款流程怎么走"  # 与目标块零关键词重合，纯语义命中
    async with AsyncSessionLocal() as s:
        hits = await retrieve(s, co, query, top_k=5, limit=200)
    assert hits and hits[0]["chunk_id"] == target_id, \
        "旧于 200 条噪声的语义命中必须靠扩大的候选池捞回"

    # 对照：无向量路径时保持 limit=200 的旧语义，捞不到就是捞不到
    monkeypatch.setattr(kb_embedding, "embed_texts", _none_embed)
    async with AsyncSessionLocal() as s:
        assert await retrieve(s, co, query, top_k=5, limit=200) == []


# ------------------------------------------------------- embed_texts 本体

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        return self._responder(json)


async def test_embed_texts_request_shape_and_parsing(tenants, monkeypatch):
    """OpenAI 兼容 /embeddings：URL 拼法、请求体、无 Key 不带头、按 index 对位。"""
    from app import kb_embedding
    from app.services import api_key_resolver

    co = tenants.alice_co["id"]
    await _install_model(co, "http://127.0.0.1:9/v1")

    def _responder(body):
        # 按请求条数回包，但乱序：验证按响应里的 index 对位，不是到达顺序
        inputs = body["input"]
        vecs = [[0.1 * (i + 1), 0.2, 0.3, 0.4] for i in range(len(inputs))]
        items = [{"index": i, "embedding": v}
                 for i, v in reversed(list(enumerate(vecs)))]
        return _FakeResponse({"data": items})

    client = _FakeClient(_responder)
    monkeypatch.setattr(kb_embedding, "get_http_client", lambda: client)

    async with AsyncSessionLocal() as s:
        got = await kb_embedding.embed_texts(s, co, ["第一段", "第二段"])
    assert got == [[0.1, 0.2, 0.3, 0.4], [0.2, 0.2, 0.3, 0.4]], \
        "必须按响应里的 index 对位，不能按到达顺序"
    call = client.calls[0]
    assert call["url"] == "http://127.0.0.1:9/v1/embeddings"
    assert call["json"] == {"model": "emb-test-1", "input": ["第一段", "第二段"]}
    assert call["headers"] == {}, "没配密钥就不该带 Authorization 头"

    # 有密钥时走 Bearer（与 OpenAICompatProvider 同款）
    async def _fake_resolve(db, **kwargs):
        return api_key_resolver.ResolvedKey("sk-emb-test", "env")

    monkeypatch.setattr(kb_embedding.api_key_resolver, "resolve", _fake_resolve)
    async with AsyncSessionLocal() as s:
        assert await kb_embedding.embed_texts(s, co, ["x"]) == \
            [[0.1, 0.2, 0.3, 0.4]]
    assert client.calls[-1]["headers"] == {"Authorization": "Bearer sk-emb-test"}


async def test_embed_texts_degrades_on_any_failure(tenants, monkeypatch):
    """连不上 / 响应畸形 / 批内维度不一致 → 一律 None，绝不抛。"""
    from app import kb_embedding

    co = tenants.alice_co["id"]
    await _install_model(co, "http://127.0.0.1:9/v1")

    def _raising_responder(body):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(kb_embedding, "get_http_client",
                        lambda: _FakeClient(_raising_responder))
    async with AsyncSessionLocal() as s:
        assert await kb_embedding.embed_texts(s, co, ["x"]) is None

    bad_shapes = [
        {"object": "list"},                               # 缺 data
        {"data": []},                                     # 数量不符
        {"data": [{"index": 0, "embedding": "abc"}]},     # 向量不是数组
        {"data": [{"index": 5, "embedding": [1.0]}]},     # index 越界
        {"data": [{"index": 0, "embedding": [1.0, 2.0]},
                  {"index": 1, "embedding": [1.0]}]},     # 批内维度不一致
    ]
    for payload in bad_shapes:
        monkeypatch.setattr(
            kb_embedding, "get_http_client",
            lambda p=payload: _FakeClient(lambda b: _FakeResponse(p)))
        async with AsyncSessionLocal() as s:
            assert await kb_embedding.embed_texts(s, co, ["a", "b"]) is None, \
                f"畸形响应必须判死：{payload}"


async def test_company_owned_config_wins_over_shared(tenants, monkeypatch):
    """公司自有 embedding 模型优先于平台共享行。"""
    from app import kb_embedding

    co = tenants.alice_co["id"]
    await _install_model(None, "http://127.0.0.1:9/shared")
    await _install_model(co, "http://127.0.0.1:9/owned")
    client = _FakeClient(lambda body: _FakeResponse(
        {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}))
    monkeypatch.setattr(kb_embedding, "get_http_client", lambda: client)
    async with AsyncSessionLocal() as s:
        await kb_embedding.embed_texts(s, co, ["x"])
    assert client.calls[0]["url"].startswith("http://127.0.0.1:9/owned")
