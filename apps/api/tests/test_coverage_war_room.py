"""覆盖率补齐：作战室全流程（kickoff/宣告/校对/执行/审核/整合/生命周期）。

模型调用与后台投递全部 mock：本文件只验证路由与状态机分支，
不产生真实网络请求，也不真跑后台线程。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.routes import war_room as wr
from app.api.routes.group_chat import GroupChatMessage
from app.db.session import AsyncSessionLocal
from app.models.agent import AgentModelConfig
from app.models.asset import Asset
from app.models.project import ProjectMember
from app.models.war_room import WarRoomSession, WarRoomTask

pytestmark = pytest.mark.asyncio


async def _agent(client, user, company_id: str, name: str, role: str = "专家") -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": role},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _bind_model(client, user, company_id: str, agent_id: str) -> str:
    r = await client.post("/models", json={
        "name": f"模型-{uuid.uuid4().hex[:5]}", "provider_type": "openai_compat",
        "model_id": "gpt-test", "company_id": company_id},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        s.add(AgentModelConfig(agent_id=agent_id, role="primary", model_id=mid))
        await s.commit()
    return mid


async def _seed(company_id: str, leader_id: str, status: str = "announcing",
                tasks: list[dict] | None = None, **extra) -> tuple[str, list[str]]:
    sid = uuid.uuid4().hex
    ids: list[str] = []
    async with AsyncSessionLocal() as s:
        s.add(WarRoomSession(id=sid, company_id=company_id, goal="季度增长方案",
                             leader_agent_id=leader_id, status=status,
                             deliverable_rule="统一 markdown", **extra))
        for i, t in enumerate(tasks or [], start=1):
            tid = str(uuid.uuid4())
            ids.append(tid)
            s.add(WarRoomTask(
                id=tid, session_id=sid, seq=i,
                agent_id=t.get("agent_id", leader_id),
                title=t.get("title", f"任务{i}"),
                detail=t.get("detail"),
                status=t.get("status", "todo"),
                announce_status=t.get("announce_status", "pending"),
                deliverable_path=t.get("path"),
                deliverable_name=t.get("name"),
                deliverable_note=t.get("note")))
        await s.commit()
    return sid, ids


@pytest.fixture
def fake_llm(monkeypatch):
    """替换模型调用；默认返回一段可解析的正文。"""
    state = {"content": "我的任务是完成市场分析并交付文档。", "error": None, "calls": 0}

    async def _chat(self, agent, messages, tools=None, config=None):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        return {"content": state["content"]}

    monkeypatch.setattr(wr.ModelGateway, "chat_with_agent_models_by_agent", _chat)
    return state


@pytest.fixture
def queued_jobs(monkeypatch):
    """拦下后台线程投递：只记录协程，由用例决定是否/如何驱动。"""
    import app.background_jobs as bg

    jobs = []
    monkeypatch.setattr(bg, "submit_async", lambda job, *a, **k: jobs.append(job))
    return jobs


async def test_kickoff_analyzes_and_builds_roster(client, make_user, make_company,
                                                  fake_llm, queued_jobs):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "总指挥")
    writer = await _agent(client, user, co["id"], "种草文案专家", "文案")
    await _agent(client, user, co["id"], "数据分析师", "分析")
    await _bind_model(client, user, co["id"], leader["id"])

    fake_llm["content"] = json.dumps({
        "analysis": "需要内容与数据两个方向",
        "deliverable_format": "md",
        "deliverable_rule": "所有人只交 markdown",
        "tasks": [
            {"agent_name": "文案", "title": "写种草文案", "detail": "输出 3 版"},
            {"agent_name": "不存在的人", "title": "杂项", "detail": "兜底"},
            {"agent_name": "总指挥", "title": "整合", "detail": "汇总"},
        ]}, ensure_ascii=False)

    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "把新品打爆",
        "leader_agent_id": leader["id"]}, headers=h)
    assert r.status_code == 201, r.text
    sid, pid = r.json()["id"], r.json()["project_id"]
    assert r.json()["status"] == "analyzing"

    async with AsyncSessionLocal() as s:
        await queued_jobs[0](s)

    r = await client.get(f"/war-room/{sid}?company_id={co['id']}", headers=h)
    assert r.status_code == 200, r.text
    board = r.json()
    assert board["status"] == "announcing"
    assert board["deliverable_rule"] == "所有人只交 markdown"
    assert len(board["tasks"]) == 3
    # 精确/包含/关键词三级模糊匹配：文案 → 种草文案专家
    assert board["tasks"][0]["agent_id"] == writer["id"]
    # 匹配不到的人兜底给负责人，并在详情里留痕
    assert board["tasks"][1]["agent_id"] == leader["id"]
    assert "原指派：不存在的人" in board["tasks"][1]["detail"]

    # 会话列表 + 项目过滤
    r = await client.get(f"/war-room/sessions?company_id={co['id']}", headers=h)
    assert r.status_code == 200 and len(r.json()["sessions"]) == 1
    r = await client.get(
        f"/war-room/sessions?company_id={co['id']}&project_id={pid}", headers=h)
    assert len(r.json()["sessions"]) == 1
    r = await client.get(
        f"/war-room/sessions?company_id={co['id']}&project_id={uuid.uuid4().hex}",
        headers=h)
    assert r.json()["sessions"] == []

    # 团队面板：进度与角色
    r = await client.get(f"/war-room/sessions/{sid}/team?company_id={co['id']}",
                         headers=h)
    assert r.status_code == 200, r.text
    team = r.json()
    assert team["leader"]["name"] == "总指挥"
    assert {m["name"] for m in team["members"]} == {"总指挥", "种草文案专家"}
    assert team["overall_progress"]["total_tasks"] == 3
    assert team["overall_progress"]["pct"] == 0

    # 团队成员自动加入项目
    async with AsyncSessionLocal() as s:
        members = (await s.execute(select(ProjectMember).where(
            ProjectMember.project_id == pid))).scalars().all()
    assert {m.member_id for m in members} >= {leader["id"], writer["id"]}


async def test_kickoff_failure_paths_and_auto_run(client, make_user, make_company,
                                                  fake_llm, queued_jobs,
                                                  monkeypatch):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    await _agent(client, user, co["id"], "执行者")

    # 非本公司 → 404
    other = await make_company(await make_user())
    r = await client.post("/war-room/kickoff", json={
        "company_id": other["id"], "goal": "x", "leader_agent_id": leader["id"]},
        headers=h)
    assert r.status_code == 404
    # 负责人不存在 → 404
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "x", "leader_agent_id": uuid.uuid4().hex},
        headers=h)
    assert r.status_code == 404

    # 后台任务找不到会话/负责人时静默返回
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], uuid.uuid4().hex, "x", leader["id"],
                               None)(s)

    # 负责人未绑定模型 → 会话 failed 且带错误说明
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "未配模型", "leader_agent_id": leader["id"]},
        headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "未配模型", leader["id"], None)(s)
        sess = await s.get(WarRoomSession, sid)
    assert sess.status == "failed" and "未配置模型" in sess.analysis["error"]

    await _bind_model(client, user, co["id"], leader["id"])

    # 模型没返回 JSON → failed
    fake_llm["content"] = "我不会输出结构化内容"
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "无效输出", "leader_agent_id": leader["id"]},
        headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "无效输出", leader["id"], None)(s)
        sess = await s.get(WarRoomSession, sid)
    assert sess.status == "failed" and "未返回JSON" in sess.analysis["error"]

    # 拆解为空 → failed
    fake_llm["content"] = json.dumps({"analysis": "x", "tasks": []})
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "空计划", "leader_agent_id": leader["id"]},
        headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "空计划", leader["id"], None)(s)
        sess = await s.get(WarRoomSession, sid)
    assert sess.status == "failed" and "未拆解出任何任务" in sess.analysis["error"]

    # LLM 抛错 → failed
    fake_llm["error"] = RuntimeError("上游 500")
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "上游挂了", "leader_agent_id": leader["id"]},
        headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "上游挂了", leader["id"], None)(s)
        sess = await s.get(WarRoomSession, sid)
    assert sess.status == "failed" and "上游 500" in sess.analysis["error"]

    # 一句话直达：跳过逐人确认，直接 executing；投递失败则 failed
    fake_llm["error"] = None
    fake_llm["content"] = json.dumps({
        "analysis": "x", "tasks": [{"agent_name": "执行者", "title": "干活"}]},
        ensure_ascii=False)
    calls = []

    async def _dispatch(db, sess, tasks):
        calls.append(sess.id)
        return 1

    monkeypatch.setattr(wr, "_dispatch_members", _dispatch)
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "直达", "leader_agent_id": leader["id"],
        "deliverable_format": "word"}, headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "直达", leader["id"], "word", None,
                               auto_run=True)(s)
        sess = await s.get(WarRoomSession, sid)
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid))).scalars().all()
    assert calls == [sid]
    assert sess.status == "executing" and sess.auto_run is True
    assert sess.deliverable_format == "word"
    assert all(t.announce_status == "acknowledged" for t in rows)

    async def _boom(db, sess, tasks):
        raise RuntimeError("队列不可用")

    monkeypatch.setattr(wr, "_dispatch_members", _boom)
    r = await client.post("/war-room/kickoff", json={
        "company_id": co["id"], "goal": "投递失败", "leader_agent_id": leader["id"]},
        headers=h)
    sid = r.json()["id"]
    async with AsyncSessionLocal() as s:
        await wr._make_kickoff(co["id"], sid, "投递失败", leader["id"], None,
                               None, auto_run=True)(s)
        sess = await s.get(WarRoomSession, sid)
    assert sess.status == "failed"


async def test_announce_acknowledge_chat_and_snapshot(client, make_user,
                                                      make_company, fake_llm):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    a1 = await _agent(client, user, co["id"], "成员甲")
    a2 = await _agent(client, user, co["id"], "成员乙")
    sid, tids = await _seed(co["id"], leader["id"], "announcing", [
        {"agent_id": leader["id"], "announce_status": "pending"},
        {"agent_id": a1["id"]},
        {"agent_id": a2["id"]},
    ])

    fake_llm["content"] = "我的任务是：交付第一部分。"
    r = await client.post(f"/war-room/{sid}/announce?company_id={co['id']}",
                          headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["announcements"]) == 3

    r = await client.get(f"/war-room/sessions/{sid}/chat?company_id={co['id']}", headers=h)
    assert r.status_code == 200 and len(r.json()["messages"]) == 3
    assert r.json()["messages"][0]["text"].startswith("我的任务是")

    # 快照：全量 → 增量
    r = await client.get(f"/war-room/sessions/{sid}/snapshot?company_id={co['id']}",
                         headers=h)
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["session"]["status"] == "announcing"
    assert len(snap["chat"]["messages"]) == 3
    assert snap["chat"]["incremental"] is False
    latest = snap["chat"]["latest_id"]
    r = await client.get(
        f"/war-room/sessions/{sid}/snapshot?company_id={co['id']}&since_id={latest}",
        headers=h)
    assert r.json()["chat"]["incremental"] is True

    # 校对：任务不存在 / 纠正未给反馈 / 纠正 / 确认
    r = await client.post(f"/war-room/{sid}/acknowledge?company_id={co['id']}", json={
        "task_id": uuid.uuid4().hex, "correct": True}, headers=h)
    assert r.status_code == 404
    r = await client.post(f"/war-room/{sid}/acknowledge?company_id={co['id']}", json={
        "task_id": tids[1], "correct": False}, headers=h)
    assert r.status_code == 400
    r = await client.post(f"/war-room/{sid}/acknowledge?company_id={co['id']}", json={
        "task_id": tids[1], "correct": False, "feedback": "方向不对，重说"},
        headers=h)
    assert r.status_code == 200 and r.json()["all_acknowledged"] is False
    async with AsyncSessionLocal() as s:
        t = await s.get(WarRoomTask, tids[1])
        assert t.announce_status == "pending" and t.announce_text is None
        assert t.leader_feedback == "方向不对，重说"

    for tid in tids:
        r = await client.post(
            f"/war-room/{sid}/acknowledge?company_id={co['id']}", json={
                "task_id": tid, "correct": True}, headers=h)
        assert r.status_code == 200
    assert r.json()["all_acknowledged"] is True
    assert r.json()["session_status"] == "confirmed"

    r = await client.post(f"/war-room/{sid}/announce?company_id={co['id']}",
                          headers=h)
    assert r.status_code == 409


async def test_session_lifecycle_file_preview_and_batch_delete(client, tenants):
    user = tenants.alice
    co = tenants.alice_co
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    sid, tids = await _seed(co["id"], leader["id"], "confirmed", [
        {"agent_id": leader["id"], "status": "todo",
         "announce_status": "acknowledged"},
        {"status": "running", "announce_status": "acknowledged"},
        {"status": "todo", "announce_status": "acknowledged"},
    ])

    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "pause"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "pause"}, headers=h)
    assert r.status_code == 409
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "resume"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "executing"
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "resume"}, headers=h)
    assert r.status_code == 409
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "rewind"}, headers=h)
    assert r.status_code == 400
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "stop"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid))).scalars().all()
    assert {t.status for t in rows} == {"cancelled"}
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "stop"}, headers=h)
    assert r.status_code == 409

    # 文件下载：正常 / 路径穿越 / 不存在 / 跨租户会话
    root = wr._war_room_dir(sid)
    root.mkdir(parents=True, exist_ok=True)
    (root / "报告.md").write_text("# 交付内容", encoding="utf-8")
    r = await client.get(
        f"/war-room/sessions/{sid}/file?company_id={co['id']}&name=报告.md",
        headers=h)
    assert r.status_code == 200 and "# 交付内容" in r.text
    assert "markdown" in r.headers.get("content-type", "")
    r = await client.get(
        f"/war-room/sessions/{sid}/file?company_id={co['id']}&name=../secret.md",
        headers=h)
    assert r.status_code == 404
    r = await client.get(
        f"/war-room/sessions/{sid}/file?company_id={co['id']}&name=缺失.md",
        headers=h)
    assert r.status_code == 404
    r = await client.get(
        f"/war-room/sessions/{sid}/file?company_id={tenants.bob_co['id']}"
        f"&name=报告.md", headers=h)
    assert r.status_code == 404

    # 单删
    r = await client.post(f"/war-room/sessions/{sid}/lifecycle?company_id={co['id']}",
                          json={"action": "delete"}, headers=h)
    assert r.status_code == 200 and r.json()["deleted"] is True
    r = await client.get(f"/war-room/{sid}?company_id={co['id']}", headers=h)
    assert r.status_code == 404

    # 批量删除：自有成功、跨租户/不存在进 failed
    sid1, _ = await _seed(co["id"], leader["id"], "announcing")
    sid2, _ = await _seed(co["id"], leader["id"], "announcing")
    bob_sid, _ = await _seed(tenants.bob_co["id"], leader["id"], "announcing")
    missing = uuid.uuid4().hex
    r = await client.post(f"/war-room/sessions/batch-delete?company_id={co['id']}",
                          json={"session_ids": [sid1, sid2, bob_sid, missing]},
                          headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_count"] == 2
    assert {f["id"] for f in r.json()["failed"]} == {bob_sid, missing}


async def test_start_review_and_dispatch_queue_full(client, make_user, make_company,
                                                    monkeypatch):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    m1 = await _agent(client, user, co["id"], "成员甲")
    m2 = await _agent(client, user, co["id"], "成员乙")

    sid, tids = await _seed(co["id"], leader["id"], "confirmed", [
        {"agent_id": leader["id"], "status": "todo", "announce_status": "acknowledged"},
        {"agent_id": m1["id"], "status": "todo", "announce_status": "acknowledged"},
        {"agent_id": m2["id"], "status": "todo", "announce_status": "acknowledged"},
    ])
    dispatched = []
    real_dispatch = wr._dispatch_members

    async def _fake_dispatch(db, sess, tasks):
        dispatched.append([t.id for t in tasks if t.status == "todo"])
        await db.commit()
        return 2

    monkeypatch.setattr(wr, "_dispatch_members", _fake_dispatch)
    r = await client.post(f"/war-room/{sid}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 200 and r.json()["dispatched"] == 2
    r = await client.get(f"/war-room/{sid}?company_id={co['id']}", headers=h)
    assert r.json()["status"] == "executing"

    # 审核：不存在 / 驳回 / 通过（老板人工）
    r = await client.post(f"/war-room/tasks/{uuid.uuid4().hex}/review"
                          f"?company_id={co['id']}",
                          json={"approve": True}, headers=h)
    assert r.status_code == 404
    r = await client.post(f"/war-room/tasks/{tids[0]}/review?company_id={co['id']}",
                          json={"approve": False, "comment": "重做",
                                "reviewer": "boss"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "todo"
    r = await client.post(f"/war-room/tasks/{tids[1]}/review?company_id={co['id']}",
                          json={"approve": True}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "approved"

    # 全员都提交/通过后再次开工 → 409
    async with AsyncSessionLocal() as s:
        for tid in tids:
            t = await s.get(WarRoomTask, tid)
            t.status = "approved"
        await s.commit()
    r = await client.post(f"/war-room/{sid}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 409

    # 未确认分工 → 409
    sid2, _ = await _seed(co["id"], leader["id"], "announcing", [
        {"agent_id": m1["id"]}])
    r = await client.post(f"/war-room/{sid2}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 409

    # failed 且有 todo → 恢复 executing；没有 todo → 409
    sid3, _ = await _seed(co["id"], leader["id"], "failed", [
        {"agent_id": m1["id"], "status": "todo"}])
    r = await client.post(f"/war-room/{sid3}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 200
    r = await client.get(f"/war-room/{sid3}?company_id={co['id']}", headers=h)
    assert r.json()["status"] == "executing"

    sid4, _ = await _seed(co["id"], leader["id"], "failed", [
        {"agent_id": leader["id"], "status": "todo"}])
    r = await client.post(f"/war-room/{sid4}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 409

    # CAS：路由拿到的是过期的 confirmed 快照，但库里已是 executing → 409
    sid5, t5 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": m1["id"], "status": "todo"}])

    async def _stale(db, sid_, company_id):
        rows = (await db.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid_))).scalars().all()
        return SimpleNamespace(id=sid_, status="confirmed",
                               leader_agent_id=leader["id"]), list(rows)

    monkeypatch.setattr(wr, "_get_session", _stale)
    r = await client.post(f"/war-room/{sid5}/start?company_id={co['id']}", headers=h)
    assert r.status_code == 409
    monkeypatch.setattr(wr, "_get_session", wr._get_session)

    # 真 _dispatch_members：队列满的任务必须退回 todo，不能永久卡 running
    monkeypatch.setattr(wr, "_dispatch_members", real_dispatch)
    sid6, t6 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": leader["id"], "status": "todo"},
        {"agent_id": m1["id"], "status": "todo"},
        {"agent_id": m2["id"], "status": "todo"},
    ])

    import app.background_jobs as bg
    monkeypatch.setattr(bg, "submit_async", lambda job, *a, **k: bg.QUEUE_FULL)
    async with AsyncSessionLocal() as s:
        sess = await s.get(WarRoomSession, sid6)
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid6))).scalars().all()
        n = await wr._dispatch_members(s, sess, list(rows))
    assert n == 0
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid6))).scalars().all()
    # 全部退回 todo，leader 任务不参与投递
    assert all(t.status == "todo" for t in rows)

    # 没有成员 todo 时直接返回 0
    async with AsyncSessionLocal() as s:
        for t in rows:
            db_t = await s.get(WarRoomTask, t.id)
            db_t.status = "approved"
        await s.commit()
        sess = await s.get(WarRoomSession, sid6)
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid6))).scalars().all()
        assert await wr._dispatch_members(s, sess, list(rows)) == 0


async def test_make_runner_success_failure_and_auto_finalize(client, make_user,
                                                             make_company,
                                                             fake_llm, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    leader = await _agent(client, user, co["id"], "负责人")
    member = await _agent(client, user, co["id"], "执行者")

    # 成功：产物落盘 + 登记 Asset + 直达收口
    sid, tids = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "running",
         "announce_status": "acknowledged"}], auto_run=True)
    merged = []

    async def _fake_merge(db, sess, tasks):
        merged.append(sess.id)
        return {"ok": True}

    monkeypatch.setattr(wr, "_merge_and_deliver", _fake_merge)
    fake_llm["content"] = "# 第一部分\n真实成果"
    snapshot = {"id": sid, "goal": "季度增长方案"}
    async with AsyncSessionLocal() as s:
        await wr._make_runner(tids[0], member["id"], snapshot)(s)
    async with AsyncSessionLocal() as s:
        t = await s.get(WarRoomTask, tids[0])
        assert t.status == "approved" and t.deliverable_path
        assert t.review_comment == "一句话直达：自动通过"
        assert t.deliverable_name.endswith(".md")
        assert open(t.deliverable_path, encoding="utf-8").read() == fake_llm["content"]
        asset = (await s.execute(select(Asset).where(
            Asset.task_id.is_(None), Asset.agent_id == member["id"]))).scalars().all()
        assert asset and asset[-1].name == t.deliverable_name
    assert merged == [sid]

    # 失败：任务 failed，且没有别的 todo/running 时会话也 failed
    sid2, t2 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "running"}])
    fake_llm["error"] = RuntimeError("模型超时")
    async with AsyncSessionLocal() as s:
        await wr._make_runner(t2[0], member["id"], {"id": sid2, "goal": "x"})(s)
    async with AsyncSessionLocal() as s:
        t = await s.get(WarRoomTask, t2[0])
        sess = await s.get(WarRoomSession, sid2)
    assert t.status == "failed" and "执行失败" in t.deliverable_note
    assert sess.status == "failed"

    # 失败但还有 todo：会话保持 executing
    sid3, t3 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "running"},
        {"agent_id": member["id"], "status": "todo"}])
    async with AsyncSessionLocal() as s:
        await wr._make_runner(t3[0], member["id"], {"id": sid3, "goal": "x"})(s)
    async with AsyncSessionLocal() as s:
        assert (await s.get(WarRoomSession, sid3)).status == "executing"

    # 空内容同样视为失败，不能假成功
    sid4, t4 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "running"}])
    fake_llm["error"] = None
    fake_llm["content"] = "   "
    async with AsyncSessionLocal() as s:
        await wr._make_runner(t4[0], member["id"], {"id": sid4, "goal": "x"})(s)
    async with AsyncSessionLocal() as s:
        t = await s.get(WarRoomTask, t4[0])
    assert t.status == "failed" and "空内容" in t.deliverable_note

    # 渲染失败：不留下不存在的交付记录
    sid5, t5 = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "running"}])

    async def _render_boom(**kwargs):
        raise OSError("磁盘只读")

    monkeypatch.setattr(wr, "_render_and_register_file", _render_boom)
    fake_llm["content"] = "有内容"
    async with AsyncSessionLocal() as s:
        await wr._make_runner(t5[0], member["id"], {"id": sid5, "goal": "x"})(s)
    async with AsyncSessionLocal() as s:
        t = await s.get(WarRoomTask, t5[0])
    assert t.status == "failed" and t.deliverable_path is None
    assert "交付物生成失败" in t.deliverable_note

    # 任务/成员/会话不存在时静默返回
    async with AsyncSessionLocal() as s:
        await wr._make_runner(uuid.uuid4().hex, member["id"],
                              {"id": sid5, "goal": "x"})(s)


async def test_finalize_merges_approved_deliverables(client, make_user,
                                                     make_company, fake_llm):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    member = await _agent(client, user, co["id"], "执行者")

    sid, tids = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "approved",
         "note": "第一部分", "name": "1.md"},
        {"agent_id": member["id"], "status": "approved",
         "note": "缺失文件的摘要", "name": "2.md"},
        {"agent_id": member["id"], "status": "approved",
         "note": "Office 文档摘要", "name": "3.docx"},
    ])
    root = wr._war_room_dir(sid)
    root.mkdir(parents=True, exist_ok=True)
    md = root / "1.md"
    md.write_text("# 第一部分\n真实内容", encoding="utf-8")
    async with AsyncSessionLocal() as s:
        t1 = await s.get(WarRoomTask, tids[0])
        t1.deliverable_path = str(md)
        t2 = await s.get(WarRoomTask, tids[1])
        t2.deliverable_path = str(root / "缺失.md")
        t3 = await s.get(WarRoomTask, tids[2])
        t3.deliverable_path = str(root / "3.docx")
        await s.commit()

    fake_llm["content"] = "# 最终交付\n整合后的完整文档"
    r = await client.post(f"/war-room/{sid}/finalize?company_id={co['id']}",
                          headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged_parts"] == 3
    assert body["preview"].startswith("# 最终交付")
    async with AsyncSessionLocal() as s:
        sess = await s.get(WarRoomSession, sid)
        assert sess.status == "done"
        assert sess.final_output_path == body["final_output"]
        assert open(sess.final_output_path, encoding="utf-8").read() == fake_llm["content"]
        # 最终文件登记为 Asset
        assets = (await s.execute(select(Asset).where(
            Asset.agent_id == leader["id"]))).scalars().all()
        assert assets and assets[-1].size > 0

    # 没有任何已通过成果 → 409
    sid2, _ = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "submitted"}])
    r = await client.post(f"/war-room/{sid2}/finalize?company_id={co['id']}",
                          headers=h)
    assert r.status_code == 409

    # 负责人返回空内容 → 502，不产出空文件
    sid3, t3ids = await _seed(co["id"], leader["id"], "executing", [
        {"agent_id": member["id"], "status": "approved", "note": "摘要"}])
    fake_llm["content"] = ""
    r = await client.post(f"/war-room/{sid3}/finalize?company_id={co['id']}",
                          headers=h)
    assert r.status_code == 502
    async with AsyncSessionLocal() as s:
        assert (await s.get(WarRoomSession, sid3)).status == "executing"

    # _merge_and_deliver 直接调用：无通过成果 → 409
    async with AsyncSessionLocal() as s:
        sess = await s.get(WarRoomSession, sid2)
        rows = (await s.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid2))).scalars().all()
        with pytest.raises(Exception):
            await wr._merge_and_deliver(s, sess, list(rows))


async def test_boss_say_responders_and_team_progress(client, tenants,
                                                     fake_llm, monkeypatch):
    user = tenants.alice
    co = tenants.alice_co
    h = user["headers"]
    leader = await _agent(client, user, co["id"], "负责人")
    a1 = await _agent(client, user, co["id"], "成员甲")
    a2 = await _agent(client, user, co["id"], "成员乙")
    sid, tids = await _seed(co["id"], leader["id"], "announcing", [
        {"agent_id": leader["id"], "status": "approved",
         "announce_status": "acknowledged"},
        {"agent_id": a1["id"], "status": "submitted",
         "announce_status": "announced"},
        {"agent_id": a2["id"], "status": "todo", "announce_status": "pending"},
    ])

    jobs = []
    import app.background_jobs as bg
    monkeypatch.setattr(bg, "submit_async", lambda job: jobs.append(job))
    fake_llm["content"] = "收到，我马上执行。"

    r = await client.post(f"/war-room/sessions/{sid}/say?company_id={co['id']}",
                          json={"text": "大家加快进度"}, headers=h)
    assert r.status_code == 200 and r.json()["agents_responding"] == 3
    assert len(jobs) == 3
    for job in jobs:
        async with AsyncSessionLocal() as s:
            await job(s)

    r = await client.get(f"/war-room/sessions/{sid}/chat?company_id={co['id']}", headers=h)
    msgs = r.json()["messages"]
    assert msgs[0]["role"] == "boss" and msgs[0]["text"] == "大家加快进度"
    assert len([m for m in msgs if m["role"] == "agent"]) == 3
    assert any(m["text"] == "收到，我马上执行。" for m in msgs[1:])

    # 跨租户：拿自己的公司 id 读别家会话 → 404
    bob_sid, _ = await _seed(tenants.bob_co["id"], leader["id"], "announcing")
    r = await client.get(f"/war-room/{bob_sid}/chat?company_id={co['id']}", headers=h)
    assert r.status_code == 404
    r = await client.get(f"/war-room/{bob_sid}?company_id={co['id']}", headers=h)
    assert r.status_code == 404
    r = await client.get(f"/war-room/sessions?company_id={tenants.bob_co['id']}",
                         headers=h)
    assert r.status_code == 404

    # 团队进度：approved 1 / submitted 1 / todo 1 → 33%
    r = await client.get(f"/war-room/sessions/{sid}/team?company_id={co['id']}",
                         headers=h)
    prog = r.json()["overall_progress"]
    assert prog["total_tasks"] == 3
    assert prog["approved"] == 1 and prog["pct"] == 33
    assert prog["acknowledged"] == 1

    # since_id 属于其他 scope → 退回全量快照
    async with AsyncSessionLocal() as s:
        other = GroupChatMessage(
            id=uuid.uuid4().hex[:32], company_id=co["id"], scope="group",
            scope_id="另一个群", role="boss", content={"text": "别的群"})
        s.add(other)
        await s.commit()
        other_id = other.id
    r = await client.get(
        f"/war-room/sessions/{sid}/snapshot?company_id={co['id']}&since_id={other_id}",
        headers=h)
    assert r.status_code == 200
    assert r.json()["chat"]["incremental"] is False
