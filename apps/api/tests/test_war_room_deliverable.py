"""作战室交付物：必须是真文件，失败必须真失败。

覆盖两处曾经"看起来成功"的缺陷：

1. `deliverable_format=word/ppt/excel` 时，原实现把 LLM 的 markdown 原文
   `write_text` 成 `.docx/.pptx/.xlsx`，产出物打不开。现在必须走
   `app.doc_generator.generate_document` 真实渲染。
2. LLM 调用抛异常时，原实现把 `(执行失败：…)` 当交付物写盘并标 `submitted`。
   现在必须：不落任何文件、任务 `failed`、备注写明异常类型。

用例完全离线：不调真实模型（monkeypatch 网关）、数据库是 conftest 的临时
SQLite、文件一律写到 pytest 的 tmp_path。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from sqlalchemy import select

from app.api.routes import war_room
from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.asset import Asset
from app.models.war_room import WarRoomSession, WarRoomTask

LLM_MARKDOWN = "# 标题\n\n| 指标 | 数值 |\n|---|---|\n| 收入 | 100 |"


async def _make_agent(client, user, company_id, name="成员") -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "测试"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_task(company_id: str, leader_id: str, agent_id: str,
                     fmt: str = "word") -> tuple[str, str]:
    """建一场处于执行中的作战会话 + 一个 running 任务，返回 (sid, tid)。"""
    sid, tid = "sess_" + company_id[:8] + "_x", "task_" + agent_id[:8] + "_x"
    async with AsyncSessionLocal() as s:
        s.add(WarRoomSession(
            id=sid, company_id=company_id, project_id=None,
            goal="做一份市场分析", leader_agent_id=leader_id,
            status="executing", deliverable_format=fmt))
        s.add(WarRoomTask(
            id=tid, session_id=sid, seq=1, agent_id=agent_id,
            title="市场分析", detail="分析收入", status="running"))
        await s.commit()
    return sid, tid


# ---------------------------------------------------------------------------
# 1) word 交付物必须是能被 python-docx 打开的真实 docx
# ---------------------------------------------------------------------------
async def test_war_room_word_deliverable_is_real_docx(client, tenants, tmp_path):
    import docx

    alice, co = tenants.alice, tenants.alice_co
    leader = await _make_agent(client, alice, co["id"], "负责人")
    member = await _make_agent(client, alice, co["id"], "成员")
    sid, tid = await _seed_task(co["id"], leader["id"], member["id"], fmt="word")
    session_dir = tmp_path / "war_room" / sid

    async with AsyncSessionLocal() as s:
        task = await s.get(WarRoomTask, tid)
        meta = await war_room._render_war_room_deliverable(
            s, task, member["id"], LLM_MARKDOWN, "word", session_dir)
        await s.commit()
        task_status = task.status
        task_path = task.deliverable_path
        asset = (await s.execute(select(Asset).where(
            Asset.id == meta["asset_id"]))).scalar_one()

    fpath = Path(meta["file_path"])
    assert fpath.parent == session_dir.resolve(), "交付物应落在会话目录内"
    assert fpath.suffix == ".docx"
    assert fpath.read_bytes()[:2] == b"PK", "docx 必须是 zip 容器"
    assert zipfile.is_zipfile(fpath)
    doc = docx.Document(str(fpath))
    cells = [c.text for t in doc.tables for r in t.rows for c in r.cells]
    body = "\n".join([p.text for p in doc.paragraphs] + cells)
    assert "收入" in body, "表格内容必须完整写进 docx"

    assert task_status == "submitted"
    assert task_path == str(fpath)
    assert meta["file_name"] == fpath.name
    # Asset 登记：指向真实文件、二进制 MIME、挂到产出它的 Agent 上
    assert asset.path == str(fpath)
    assert asset.mime_type and "wordprocessingml" in asset.mime_type
    assert asset.size == fpath.stat().st_size
    assert asset.agent_id == member["id"]
    assert asset.task_id is None  # WarRoomTask 不是 tasks 表，不能挂 task_id


# ---------------------------------------------------------------------------
# 2) LLM 失败：不落盘、不假 submitted、状态收敛
# ---------------------------------------------------------------------------
async def test_failed_llm_writes_no_file_and_marks_failed(
        client, tenants, tmp_path, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    leader = await _make_agent(client, alice, co["id"], "负责人")
    member = await _make_agent(client, alice, co["id"], "成员")
    sid, tid = await _seed_task(co["id"], leader["id"], member["id"], fmt="word")

    session_dir = tmp_path / "war_room" / sid
    monkeypatch.setattr(war_room, "_war_room_dir", lambda _sid: session_dir)

    async def _boom(self, agent, messages, tools=None, config=None):
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models_by_agent", _boom)

    runner = war_room._make_runner(
        tid, member["id"],
        {"id": sid, "company_id": co["id"], "goal": "做一份市场分析",
         "format": "word"})
    async with AsyncSessionLocal() as s:
        await runner(s)   # 后台 worker 的真实入口

    async with AsyncSessionLocal() as s:
        task = await s.get(WarRoomTask, tid)
        assets = (await s.execute(select(Asset))).scalars().all()
        sess = await s.get(WarRoomSession, sid)

    assert task.status == "failed"
    assert task.deliverable_path is None
    assert task.deliverable_name is None
    assert task.deliverable_note.startswith("执行失败：")
    assert "RuntimeError" in task.deliverable_note
    assert "upstream 502" in task.deliverable_note
    assert assets == [], "失败时不应登记任何 Asset"
    assert not session_dir.exists() or not any(session_dir.iterdir()), \
        "失败时不应写出任何交付文件"
    # 最后一个成员任务失败后，会话不能永远挂着「并行执行中」
    assert sess.status == "failed"


# ---------------------------------------------------------------------------
# 3) 驳回后重做：不需要用户重新确认，也不重复投递
# ---------------------------------------------------------------------------
async def test_rejected_task_rerun_needs_no_reconfirm(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    leader = await _make_agent(client, alice, co["id"], "负责人")
    member = await _make_agent(client, alice, co["id"], "成员")
    sid, tid = await _seed_task(co["id"], leader["id"], member["id"], fmt="md")

    async with AsyncSessionLocal() as s:   # 先做成"已提交待审核"
        t = await s.get(WarRoomTask, tid)
        t.status = "submitted"
        await s.commit()

    r = await client.post(
        f"/war-room/tasks/{tid}/review?company_id={co['id']}",
        json={"approve": False, "comment": "数据口径不对，重做"},
        headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "todo"

    submitted: list = []
    monkeypatch.setattr("app.background_jobs.submit_async",
                        lambda fn, *a, **kw: submitted.append(fn))

    r = await client.post(f"/war-room/{sid}/start?company_id={co['id']}",
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] == 1
    assert len(submitted) == 1
    async with AsyncSessionLocal() as s:
        assert (await s.get(WarRoomTask, tid)).status == "running"

    # 再点一次：没有 todo 任务，409 且不重复投递
    r = await client.post(f"/war-room/{sid}/start?company_id={co['id']}",
                          headers=alice["headers"])
    assert r.status_code == 409, r.text
    assert len(submitted) == 1
