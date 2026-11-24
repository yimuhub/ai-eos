"""阶段 D / F21：性能热点的行为与复杂度护栏。

全部数据库访问沿用 conftest 的会话级临时 SQLite；不读取真实库。
断言同时锁定结果正确性和查询/线程边界，避免只测“接口没报错”。
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.db.session import AsyncSessionLocal, engine
from app.models.agent import Agent, AgentStatus, Model, ModelProviderType
from app.models.asset import Asset
from app.models.computer_device import ComputerDevice
from app.models.performance import PerformanceSnapshot
from app.models.task import Task
from app.models.token_usage import PricingConfig, TokenUsage
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _id() -> str:
    return str(uuid.uuid4())


@contextmanager
def _capture_selects():
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, params, context,
                              executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute",
                     before_cursor_execute)


async def test_pricing_names_are_batched_and_keep_fallback(tenants):
    from app.api.routes.tokens import list_pricing

    co = tenants.alice_co["id"]
    model_ids = [_id() for _ in range(8)]
    missing_id = _id()
    async with AsyncSessionLocal() as db:
        db.add_all([
            Model(id=mid, company_id=co, name=f"模型-{i}",
                  provider_type=ModelProviderType.OPENAI_COMPAT,
                  model_id=f"test-{i}")
            for i, mid in enumerate(model_ids)
        ])
        db.add_all([
            PricingConfig(id=_id(), model_id=mid, model_name="旧快照")
            for mid in model_ids
        ])
        db.add(PricingConfig(id=_id(), model_id=missing_id,
                             model_name="已删除模型的快照"))
        await db.commit()

        with _capture_selects() as statements:
            rows = await list_pricing(db=db, user=None)

    assert len(statements) == 2, statements  # 标价一条 + 模型名称一条
    names = {r["model_id"]: r["model_name"] for r in rows}
    assert [names[mid] for mid in model_ids] == [f"模型-{i}" for i in range(8)]
    assert names[missing_id] == "已删除模型的快照"


async def test_usage_name_lookup_is_one_query_for_many_agents(tenants):
    from app.api.routes.tokens import _attach_names

    co = tenants.alice_co["id"]
    ids = [_id() for _ in range(8)]
    async with AsyncSessionLocal() as db:
        db.add_all([Agent(id=aid, company_id=co, name=f"员工-{i}")
                    for i, aid in enumerate(ids)])
        await db.commit()
        rows = [{"key": aid} for aid in ids]
        rows += [{"key": _id()}, {"key": "(未归属)"}]
        with _capture_selects() as statements:
            result = await _attach_names(db, rows, "agent")

    assert len(statements) == 1, statements
    assert [r["name"] for r in result[:8]] == [f"员工-{i}" for i in range(8)]
    assert result[8]["name"] == result[8]["key"]
    assert result[9]["name"] == "(未归属)"


async def test_task_top_n_is_limited_in_sql(tenants):
    from app.api.routes.tokens import stats_by_task

    co = tenants.alice_co["id"]
    totals = [10, 50, 30, 40, 20]
    task_ids = [_id() for _ in totals]
    async with AsyncSessionLocal() as db:
        db.add_all([Task(id=tid, company_id=co, title=f"任务-{i}",
                         created_by=tenants.alice["id"])
                    for i, tid in enumerate(task_ids)])
        db.add_all([TokenUsage(id=_id(), company_id=co, task_id=tid,
                               total_tokens=total)
                    for tid, total in zip(task_ids, totals)])
        await db.commit()
        user = await db.get(User, tenants.alice["id"])
        with _capture_selects() as statements:
            rows = await stats_by_task(company_id=co, days=30, limit=2,
                                       db=db, user=user)

    assert [r["key"] for r in rows] == [task_ids[1], task_ids[3]]
    assert [r["name"] for r in rows] == ["任务-1", "任务-3"]
    grouped = [s for s in statements if "GROUP BY" in s.upper()
               and "token_usages" in s.lower()]
    assert grouped and "LIMIT" in grouped[0].upper(), statements


async def test_hr_overview_aggregates_status_without_loading_agent_rows(
        tenants, monkeypatch):
    from app.api import deps
    from app.api.routes import performance as perf

    co = tenants.alice_co["id"]
    now = datetime(2026, 11, 9, 0, 30)
    monkeypatch.setattr(perf, "utc_naive_now", lambda: now)

    async def owned(db, user_id, company_id):
        return None  # 本用例只测统计查询，权限链路由既有契约测试覆盖

    monkeypatch.setattr(deps, "assert_company_owned", owned)
    async with AsyncSessionLocal() as db:
        ids = [_id(), _id()]
        db.add_all([Agent(id=ids[0], company_id=co, name="成本王",
                          status=AgentStatus.ACTIVE),
                    Agent(id=ids[1], company_id=co, name="产出王",
                          status=AgentStatus.IDLE)])
        db.add_all([
            TokenUsage(company_id=co, agent_id=ids[0], total_tokens=100,
                       total_cost=1.5, created_at=datetime(2026, 11, 1)),
            TokenUsage(company_id=co, agent_id=ids[1], total_tokens=7,
                       total_cost=0.5, created_at=datetime(2026, 11, 2)),
            Task(company_id=co, title="已完成", assigned_agent_id=ids[1],
                 created_by=tenants.alice["id"], status="COMPLETED",
                 updated_at=now - timedelta(days=1)),
            Task(company_id=co, title="未分派完成",
                 created_by=tenants.alice["id"], status="COMPLETED",
                 updated_at=now - timedelta(days=1)),
            PerformanceSnapshot(id=_id(), company_id=co, agent_id=ids[0],
                                period_start=now - timedelta(days=30),
                                period_end=now, score=91),
        ])
        await db.commit()
        user = await db.get(User, tenants.alice["id"])
        with _capture_selects() as first_queries:
            first = await perf.hr_overview(co, db=db, user=user)

        db.add_all([Agent(id=_id(), company_id=co, name=f"新增-{i}",
                          status=AgentStatus.IDLE) for i in range(10)])
        await db.commit()
        with _capture_selects() as second_queries:
            second = await perf.hr_overview(co, db=db, user=user)

    assert len(first_queries) == len(second_queries) == 7, second_queries
    status_query = next(s for s in second_queries if "GROUP BY" in s.upper()
                        and "agents" in s.lower())
    assert status_query
    assert first["headcount"] == {"total": 2,
                                  "by_status": {"ACTIVE": 1, "IDLE": 1}}
    assert second["headcount"] == {"total": 12,
                                   "by_status": {"ACTIVE": 1, "IDLE": 11}}
    assert second["cost_month"]["tokens"] == 107
    assert second["cost_month"]["top"][0] == {
        "agent": "成本王", "tokens": 100, "cost": 1.5}
    assert second["output_30d"]["total_tasks"] == 2
    assert second["output_30d"]["top"][0]["agent"] == "产出王"
    assert second["assessment_top"][0]["agent"] == "成本王"


async def test_dashboard_agent_total_comes_from_single_grouped_query(tenants):
    from app.api.routes.dashboard import _dashboard_payload
    from app.models.company import Company

    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as db:
        db.add_all([
            Agent(company_id=co, name="甲", status=AgentStatus.ACTIVE),
            Agent(company_id=co, name="乙", status=AgentStatus.IDLE),
            Agent(company_id=co, name="丙", status=AgentStatus.DISABLED),
        ])
        await db.commit()
        company = await db.get(Company, co)
        ctx = SimpleNamespace(company_id=co, user_id=tenants.alice["id"],
                              sees_all_agents=True)
        with _capture_selects() as statements:
            payload = await _dashboard_payload(db, co, company, ctx)

    agent_queries = [s for s in statements if "FROM agents" in s]
    assert len(agent_queries) == 1, statements
    assert "GROUP BY" in agent_queries[0].upper()
    assert payload["agents"] == {"active": 1, "idle": 1, "running": 0,
                                 "paused": 0, "disabled": 1, "error": 0,
                                 "total": 3}


async def test_chat_document_render_runs_off_event_loop_with_scalars(
        tenants, monkeypatch):
    from app.api.routes import agent_chat
    from app.model_gateway import ModelGateway
    from app.models.asset import AssetType

    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co, name="文档员", role="文秘")
        db.add(agent)
        await db.commit()
        user = await db.get(User, tenants.alice["id"])
        agent_id = str(agent.id)
        main_thread = threading.get_ident()
        calls = []

        def fake_generate(doc_type, title, content, *, agent_id):
            calls.append((doc_type, title, content, agent_id,
                          threading.get_ident()))
            return {"file_path": "/tmp/stage-d-doc.md", "file_name": "文档.md",
                    "size": 12, "mime": "text/markdown"}

        async def visible(db, user, aid):
            return agent

        async def no_style(db, agent):
            return None

        async def save_msg(*args, **kwargs):
            return None

        class FakeGateway:
            def __init__(self, db):
                self.db = db

            async def chat_with_agent_models(self, model_ids, messages):
                return {"content": "# 正文"}

        monkeypatch.setattr(agent_chat, "_visible_agent", visible)
        monkeypatch.setattr(agent_chat, "_save_msg", save_msg)
        monkeypatch.setattr(ModelGateway, "chat_with_agent_models",
                            FakeGateway.chat_with_agent_models)
        monkeypatch.setattr("app.doc_generator.generate_document", fake_generate)
        monkeypatch.setattr("app.public_doc_skill.get_bound_style", no_style)
        monkeypatch.setattr("app.public_doc_skill.resolve_style",
                            lambda *args, **kwargs: {"style_name": "通用",
                                                      "hint": "简洁"})
        result = await agent_chat.chat_generate_doc(
            agent_id, agent_chat.GenDocIn(doc_type="md", title="文档",
                                           instruction="写正文"),
            db=db, user=user)
        asset = (await db.execute(select(Asset).where(
            Asset.agent_id == agent_id))).scalar_one()

    assert result["file"]["file_name"] == "文档.md"
    assert calls == [("md", "文档", "# 正文", agent_id, calls[0][4])]
    assert calls[0][4] != main_thread
    assert asset.company_id == co and asset.type == AssetType.OTHER
    assert asset.path == "/tmp/stage-d-doc.md"


async def test_group_stream_heartbeat_and_document_thread_boundary(
        client, tenants, monkeypatch):
    from app.api.routes import group_chat
    from app.model_gateway import ModelGateway

    co = tenants.alice_co["id"]
    created = await client.post("/agents", json={
        "company_id": co, "name": "群聊文档员"}, headers=tenants.alice["headers"])
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    group = await client.post("/chat-groups", json={
        "company_id": co, "name": "文档性能群",
        "agent_ids": [agent_id]}, headers=tenants.alice["headers"])
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    marker = ('[GENERATE_DOC]{"type":"md","title":"群文档",'
              '"content":"# 群正文"}[/GENERATE_DOC]')
    render_threads = []

    async def fake_stream(self, agent, messages, tools=None, config=None):
        await asyncio.sleep(0.04)
        yield {"type": "model", "model_id": "fake", "model_name": "fake"}
        yield {"type": "delta", "text": marker}
        yield {"type": "done", "tool_calls": [], "model": "fake",
               "used_model_id": "fake", "elapsed_ms": 40}

    def fake_generate(doc_type, title, content, *, agent_id):
        render_threads.append(threading.get_ident())
        return {"file_path": "/tmp/group-doc.md", "file_name": "群文档.md",
                "size": 10, "mime": "text/markdown"}

    monkeypatch.setattr(group_chat, "GROUP_CHAT_HEARTBEAT_S", 0.01)
    monkeypatch.setattr(ModelGateway, "stream_with_agent_models_by_agent",
                        fake_stream)
    monkeypatch.setattr("app.doc_generator.generate_document", fake_generate)
    response = await client.post(
        f"/group-chat/group/{group_id}/stream?company_id={co}",
        json={"message": "生成文档"}, headers=tenants.alice["headers"])

    assert response.status_code == 200, response.text
    events = [json.loads(line[5:].strip()) for line in response.text.splitlines()
              if line.startswith("data:")]
    assert events[0]["type"] == "group_start"
    assert events[-1]["type"] == "group_done"
    assert any(e["type"] == "status" for e in events)
    assert any("群文档.md" in e.get("text", "") for e in events
               if e["type"] == "agent_done")
    assert render_threads and render_threads[0] != threading.get_ident()


async def test_delivery_zip_is_offloaded_and_bounded(client, tenants, monkeypatch):
    from app.api.routes import assets, delivery

    co = tenants.alice_co["id"]
    agent = await client.post("/agents", json={
        "company_id": co, "name": "打包员"}, headers=tenants.alice["headers"])
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]
    root = assets.storage_root().resolve()
    async with AsyncSessionLocal() as db:
        for i in range(2):
            path = root / "artifacts" / f"stage-d-{uuid.uuid4().hex}.md"
            path.write_text(f"文件{i}", encoding="utf-8")
            db.add(Asset(company_id=co, agent_id=agent_id, name=f"文件{i}.md",
                         path=str(path.relative_to(root)), size=6,
                         creator_type="agent", created_by=agent_id))
        await db.commit()

    real_build = delivery._build_zip
    build_threads = []

    def recording_build(entries):
        build_threads.append(threading.get_ident())
        return real_build(entries)

    monkeypatch.setattr(delivery, "_build_zip", recording_build)
    url = f"/delivery/zip?company_id={co}&agent_id={agent_id}"
    headers = tenants.alice["headers"]
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["文件0.md", "文件1.md"]
        assert {archive.read(n).decode() for n in archive.namelist()} == {"文件0", "文件1"}
    assert build_threads and build_threads[0] != threading.get_ident()

    monkeypatch.setattr(delivery, "MAX_ZIP_FILES", 1)
    response = await client.get(url, headers=headers)
    assert response.status_code == 413, response.text
    monkeypatch.setattr(delivery, "MAX_ZIP_FILES", 200)
    monkeypatch.setattr(delivery, "MAX_ZIP_UNCOMPRESSED_BYTES", 5)
    response = await client.get(url, headers=headers)
    assert response.status_code == 413, response.text


async def test_artifact_zip_is_offloaded_and_preserves_versions(
        client, tenants, monkeypatch):
    from app.api.routes import artifacts

    co = tenants.alice_co["id"]
    created = await client.post(f"/artifacts?company_id={co}", json={
        "name": "成果归档", "content": "第一版"},
        headers=tenants.alice["headers"])
    assert created.status_code == 200, created.text
    artifact_id = created.json()["id"]
    build_threads = []
    real_build = artifacts._build_artifact_zip

    def recording_build(*args):
        build_threads.append(threading.get_ident())
        return real_build(*args)

    monkeypatch.setattr(artifacts, "_build_artifact_zip", recording_build)
    response = await client.get(f"/artifacts/{artifact_id}/download.zip",
                                headers=tenants.alice["headers"])
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("v1.txt").decode() == "第一版"
        assert json.loads(archive.read("meta.json"))["current_version"] == 1
    assert build_threads and build_threads[0] != threading.get_ident()


async def test_upload_disk_writes_are_offloaded(tmp_path, monkeypatch):
    from app.core import uploads

    class ChunkUpload:
        size = 3

        async def read(self, size):
            if not hasattr(self, "sent"):
                self.sent = True
                return b"abc"
            return b""

    class RecordingFile:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            self.fh.__enter__()
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def write(self, chunk):
            write_threads.append(threading.get_ident())
            return self.fh.write(chunk)

    write_threads = []
    real_open = type(tmp_path).open

    def recording_open(path, *args, **kwargs):
        fh = real_open(path, *args, **kwargs)
        return RecordingFile(fh) if args and args[0] == "wb" else fh

    monkeypatch.setattr(type(tmp_path), "open", recording_open)
    path, size = await uploads.stream_upload(
        ChunkUpload(), tmp_path, "upload.txt", max_bytes=10)
    assert size == 3 and path.read_bytes() == b"abc"
    assert write_threads and write_threads[0] != threading.get_ident()


async def test_agent_runtime_document_render_runs_off_event_loop(
        tenants, monkeypatch):
    from app.agent_runtime import run_agent_on_task
    from app.model_gateway import ModelGateway

    co = tenants.alice_co["id"]
    agent_id, task_id = _id(), _id()
    marker = ('[GENERATE_DOC]{"type":"md","title":"运行时文档",'
              '"content":"# 正文"}[/GENERATE_DOC]')
    render_threads = []

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        return {"content": marker}

    def fake_generate(doc_type, title, content, *, agent_id):
        render_threads.append(threading.get_ident())
        return {"file_path": "/tmp/runtime-doc.md", "file_name": "运行时文档.md",
                "size": 10, "mime": "text/markdown"}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)
    monkeypatch.setattr("app.doc_generator.generate_document", fake_generate)
    async with AsyncSessionLocal() as db:
        agent = Agent(id=agent_id, company_id=co, name="任务文档员")
        task = Task(id=task_id, company_id=co, title="生成运行时文档",
                    assigned_agent_id=agent_id, created_by=tenants.alice["id"])
        db.add_all([agent, task])
        await db.commit()
        execution = await run_agent_on_task(db, agent, task)
        await db.commit()
        asset = (await db.execute(select(Asset).where(
            Asset.task_id == task_id, Asset.agent_id == agent_id,
            Asset.name == "运行时文档.md"))).scalar_one()

    assert execution.status == "completed"
    assert asset.name == "运行时文档.md" and asset.path == "/tmp/runtime-doc.md"
    assert render_threads and render_threads[0] != threading.get_ident()


async def test_queue_heartbeat_only_renews_owned_running_lease(monkeypatch):
    from app.models.queue_item import QueueItem, QueueItemStatus
    from app.services import job_queue

    now = datetime(2026, 11, 9, 12, 0)
    monkeypatch.setattr(job_queue, "_now", lambda: now)
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1")
        await db.commit()
        await job_queue.claim_item(db, item.id, "worker-a", lease_s=60)

        assert await job_queue.heartbeat(db, item.id, "worker-a", lease_s=30) is True
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.lease_expires_at == now + timedelta(seconds=30)
        assert row.status == QueueItemStatus.RUNNING

        assert await job_queue.heartbeat(db, item.id, "worker-b", lease_s=90) is False
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.lease_expires_at == now + timedelta(seconds=30)
        assert row.claimed_by == "worker-a"

        await job_queue.complete(db, item.id)
        assert await job_queue.heartbeat(db, item.id, "worker-a", lease_s=90) is False
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.status == QueueItemStatus.SUCCEEDED
        assert row.lease_expires_at is None


async def test_queue_heartbeat_clamps_nonpositive_lease(monkeypatch):
    from app.models.queue_item import QueueItem
    from app.services import job_queue

    now = datetime(2026, 11, 9, 12, 0)
    monkeypatch.setattr(job_queue, "_now", lambda: now)
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1")
        await db.commit()
        await job_queue.claim_item(db, item.id, "worker-a")
        assert await job_queue.heartbeat(db, item.id, "worker-a", lease_s=0)
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.lease_expires_at == now + timedelta(seconds=1)
        assert await job_queue.heartbeat(db, item.id, "worker-a", lease_s=-5)
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.lease_expires_at == now + timedelta(seconds=1)


async def test_device_heartbeat_preserves_omitted_facts(tenants, monkeypatch):
    from app.services import computer_dispatch

    now = datetime(2026, 11, 9, 12, 0)
    monkeypatch.setattr(computer_dispatch, "_now", lambda: now)
    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as db:
        device = ComputerDevice(company_id=co, name="Mac", platform="darwin",
                                hostname="mac-local", agent_version="1.2",
                                capabilities=["run_shell"])
        db.add(device)
        await db.commit()
        result = await computer_dispatch.heartbeat(db, device)
        await db.refresh(device)

    assert result == {"online": True, "server_time": now.isoformat(),
                      "capabilities": ["run_shell"]}
    assert device.last_seen_at == now
    assert (device.name, device.platform, device.hostname,
            device.agent_version) == ("Mac", "darwin", "mac-local", "1.2")
