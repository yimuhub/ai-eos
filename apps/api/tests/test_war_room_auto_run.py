"""一句话直达的收口护栏：全员交付后必须自动整合出最终文件。

改造前的死结：直达模式把任务派下去后，会话停在 executing，
"整合交付"按钮只在人工审核后出现 —— 直达流程永远拿不到最终产出。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.routes import war_room as wr
from app.db.session import AsyncSessionLocal
from app.models.war_room import WarRoomSession, WarRoomTask

pytestmark = pytest.mark.asyncio


async def _seed(sid: str, company_id: str, auto_run: bool,
                statuses: list[str]) -> None:
    async with AsyncSessionLocal() as s:
        s.add(WarRoomSession(id=sid, company_id=company_id, goal="写一份季报",
                             leader_agent_id=str(uuid.uuid4()), status="executing",
                             auto_run=auto_run))
        for i, st in enumerate(statuses, start=1):
            s.add(WarRoomTask(id=str(uuid.uuid4()), session_id=sid, seq=i,
                              agent_id=str(uuid.uuid4()), title=f"第{i}部分",
                              status=st, announce_status="acknowledged"))
        await s.commit()


@pytest.fixture
def captured_merge(monkeypatch):
    """替换真整合（会调 LLM + 渲染文件），只记录被调用过。"""
    calls: list[str] = []

    async def _fake_merge(db, sess, tasks):
        calls.append(sess.id)
        approved = [t for t in tasks if t.status == "approved"]
        sess.status = "done"
        await db.commit()
        return {"ok": True, "approved": len(approved)}

    monkeypatch.setattr(wr, "_merge_and_deliver", _fake_merge)
    return calls


async def test_auto_run_finalizes_when_all_submitted(make_company, make_user,
                                                     captured_merge):
    user = await make_user()
    co = await make_company(user)
    sid = str(uuid.uuid4())
    await _seed(sid, co["id"], auto_run=True, statuses=["submitted", "submitted"])

    async with AsyncSessionLocal() as s:
        await wr._maybe_auto_finalize(s, sid)

    assert captured_merge == [sid], "直达模式全员交付后没有自动整合"
    async with AsyncSessionLocal() as s:
        sess = await s.get(WarRoomSession, sid)
        rows = (await s.execute(
            select(WarRoomTask).where(WarRoomTask.session_id == sid))).scalars().all()
    assert sess.status == "done"
    assert all(t.status == "approved" for t in rows), "自动审核没有把成果置为已通过"


async def test_auto_run_waits_for_running_tasks(make_company, make_user,
                                                captured_merge):
    user = await make_user()
    co = await make_company(user)
    sid = str(uuid.uuid4())
    await _seed(sid, co["id"], auto_run=True, statuses=["submitted", "running"])

    async with AsyncSessionLocal() as s:
        await wr._maybe_auto_finalize(s, sid)

    assert captured_merge == [], "还有人在跑就提前整合，会漏掉最后一份成果"


async def test_auto_run_does_not_swallow_failures(make_company, make_user,
                                                  captured_merge):
    user = await make_user()
    co = await make_company(user)
    sid = str(uuid.uuid4())
    await _seed(sid, co["id"], auto_run=True, statuses=["submitted", "failed"])

    async with AsyncSessionLocal() as s:
        await wr._maybe_auto_finalize(s, sid)

    assert captured_merge == [], "有成员失败时不该自动出一份缺块的'最终交付'"


async def test_manual_session_is_not_auto_finalized(make_company, make_user,
                                                    captured_merge):
    """人工作战流程的审核点是老板的控制权，不能被自动通过。"""
    user = await make_user()
    co = await make_company(user)
    sid = str(uuid.uuid4())
    await _seed(sid, co["id"], auto_run=False, statuses=["submitted", "submitted"])

    async with AsyncSessionLocal() as s:
        await wr._maybe_auto_finalize(s, sid)

    assert captured_merge == []
    async with AsyncSessionLocal() as s:
        sess = await s.get(WarRoomSession, sid)
        assert sess.status == "executing"
