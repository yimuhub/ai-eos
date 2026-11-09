"""绩效/知识库 P1 正确性回归。

锁定四件容易悄悄退化的事：
* HR 看板的本月/近 30 天窗口必须按 UTC 算（库里是 naive UTC）；
* 绩效快照的查询数不随 Agent 数量增长（消除 N+1）；
* RAG 候选在 SQL 层必须显式排序，不能取到任意子集；
* 入库失败必须回滚半批 chunk，且文档标记 FAILED（不再 commit 待回滚会话）；
* 视频模型的默认分辨率声明与上游真实能力一致（720p）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from app.db.session import AsyncSessionLocal, engine

pytestmark = pytest.mark.asyncio


async def test_hr_overview_month_window_uses_utc(client, tenants, monkeypatch):
    """冻结到 UTC 11 月 9 日后，10 月 31 日 23:00(UTC) 的用量不能算进本月。"""
    from app.api.routes import performance as perf_mod
    from app.models.token_usage import TokenUsage

    co = tenants.alice_co
    monkeypatch.setattr(perf_mod, "utc_naive_now",
                        lambda: datetime(2026, 11, 9, 0, 30))
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(company_id=co["id"], total_tokens=100,
                         total_cost=1.0,
                         created_at=datetime(2026, 10, 31, 23, 0)))
        s.add(TokenUsage(company_id=co["id"], total_tokens=7,
                         total_cost=0.5,
                         created_at=datetime(2026, 11, 1, 0, 10)))
        await s.commit()

    r = await client.get(f"/performance/company/{co['id']}/hr-overview",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cost_month"]["tokens"] == 7
    assert body["cost_month"]["cost"] == 0.5


async def test_snapshot_query_count_is_constant(client, tenants):
    """8 个 Agent 的快照仍应是固定条数聚合查询，而不是 1 + 4×8。"""
    from app.models.agent import Agent
    from app.performance_engine import snapshot_company

    co = tenants.alice_co
    async with AsyncSessionLocal() as s:
        for i in range(8):
            s.add(Agent(company_id=co["id"], name=f"员工{i}"))
        await s.commit()

    counter = {"selects": 0}

    def before_cursor_execute(conn, cursor, statement, params, context,
                              executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute",
                 before_cursor_execute)
    try:
        async with AsyncSessionLocal() as s:
            ranked = await snapshot_company(s, co["id"])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute",
                     before_cursor_execute)

    assert len(ranked) == 8
    assert counter["selects"] <= 6, counter


async def test_retrieve_candidates_are_ordered_newest_first(tenants):
    """250 块噪声 + 1 块最新命中：limit=200 时最新命中必须在候选里。"""
    from app.knowledge_engine import retrieve
    from app.models.knowledge import KBDocument, KBDocumentStatus, KBChunk

    co = tenants.alice_co
    async with AsyncSessionLocal() as s:
        doc = KBDocument(id=str(uuid.uuid4()), company_id=co["id"],
                         title="大文档", content="x",
                         source_type="text", status=KBDocumentStatus.READY)
        s.add(doc)
        await s.flush()
        base = datetime(2026, 1, 1)
        for i in range(250):
            s.add(KBChunk(id=str(uuid.uuid4()), document_id=doc.id,
                          company_id=co["id"], seq=i, content="普通段落",
                          keywords=["普通"],
                          created_at=base + timedelta(seconds=i)))
        s.add(KBChunk(id=str(uuid.uuid4()), document_id=doc.id,
                      company_id=co["id"], seq=999, content="量子计算是重点",
                      keywords=None, created_at=datetime(2026, 2, 1)))
        await s.commit()

    async with AsyncSessionLocal() as s:
        hits = await retrieve(s, co["id"], "量子计算", top_k=5, limit=200)
    assert hits and hits[0]["seq"] == 999


async def test_ingest_failure_rolls_back_partial_chunks(tenants, monkeypatch):
    """切块中途失败：半批 chunk 必须撤销，文档标 FAILED，会话仍可用。"""
    from app import knowledge_engine as ke
    from app.models.knowledge import KBDocument, KBDocumentStatus, KBChunk

    co = tenants.alice_co
    long_a, long_b = "甲" * 600, "乙" * 600
    async with AsyncSessionLocal() as s:
        doc = KBDocument(id=str(uuid.uuid4()), company_id=co["id"],
                         title="坏文档",
                         content=f"{long_a}\n\n{long_b}", source_type="text",
                         status=KBDocumentStatus.PROCESSING)
        s.add(doc)
        await s.commit()
        doc_id = doc.id

    real_tokens = ke._tokens
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom")
        return real_tokens(text)

    monkeypatch.setattr(ke, "_tokens", flaky)

    async with AsyncSessionLocal() as s:
        doc = await s.get(KBDocument, doc_id)
        with pytest.raises(RuntimeError):
            await ke.ingest_document(s, doc)
        # 回滚后会话必须还能查：旧实现会留下待回滚事务
        chunks = (await s.execute(
            select(func.count()).select_from(KBChunk))).scalar()
        assert chunks == 0

    async with AsyncSessionLocal() as s:
        doc = await s.get(KBDocument, doc_id)
        assert doc.status == KBDocumentStatus.FAILED


async def test_video_default_resolutions_are_honest():
    """上游没声明能力时，默认只承诺 720p，避免配置页虚标 1080p。"""
    from app.model_discovery import default_config_for

    cfg = default_config_for("video")
    assert cfg["resolutions"] == ["720p"]
