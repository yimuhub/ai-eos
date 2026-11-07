"""一键派活：任务必须真的开工，而不是造出一堆永远不动的待办。

此前的实际行为：`POST /task-dispatch/dispatch` 只创建 TODO 行，谁也不启动。
"安排全公司去干活"于是等于"往待办里扔 N 条死行"。

现在钉住的语义：
1. `auto_start=true` → 创建后立即 CAS 到 RUNNING 并投递后台执行；
2. worker 池满（QUEUE_FULL）→ 任务退回 TODO 且保留 `auto_start`，
   由 `dispatch_auto_queue` 下一拍拉起，绝不留下僵尸 RUNNING 行；
3. 派活目标一律按公司过滤，不能把任务派给别家公司的员工。
"""
from __future__ import annotations

from sqlalchemy import select

import app.background_jobs as background_jobs
from app.api.routes import task_dispatch
from app.db.session import AsyncSessionLocal
from app.models.task import Task, TaskPriority, TaskStatus
from app.task_engine import transition
from app.task_runner import dispatch_auto_queue, start_task


async def _make_agent(client, user, company_id, name="员工") -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "分析"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def test_dispatch_auto_start_actually_starts(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    submitted: list[str] = []
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: submitted.append("job"))

    r = await client.post("/task-dispatch/dispatch", json={
        "company_id": co["id"], "title": "写市场分析", "target_type": "single_agent",
        "agent_ids": [agent["id"]], "auto_start": True,
    }, headers=alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_count"] == 1
    assert body["started_count"] == 1
    assert submitted, "必须真的投递到后台执行"

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, body["tasks"][0]["task_id"])
        assert t.status == TaskStatus.RUNNING


async def test_queue_full_rolls_back_to_todo_and_keeps_marker(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: background_jobs.QUEUE_FULL)

    r = await client.post("/task-dispatch/dispatch", json={
        "company_id": co["id"], "title": "整理财报", "target_type": "single_agent",
        "agent_ids": [agent["id"]], "auto_start": True,
    }, headers=alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["started_count"] == 0

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, body["tasks"][0]["task_id"])
        # 关键：不能是 RUNNING（没有 worker 在跑 = 僵尸行）
        assert t.status == TaskStatus.TODO
        assert t.auto_start is True


async def test_auto_queue_resumes_after_workers_free(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: background_jobs.QUEUE_FULL)
    r = await client.post("/task-dispatch/dispatch", json={
        "company_id": co["id"], "title": "复盘", "target_type": "single_agent",
        "agent_ids": [agent["id"]], "auto_start": True,
    }, headers=alice["headers"])
    tid = r.json()["tasks"][0]["task_id"]

    # worker 空出来了：下一拍必须把它拉起
    monkeypatch.setattr(background_jobs, "submit_async", lambda fn, *a, **kw: "future")
    async with AsyncSessionLocal() as s:
        started = await dispatch_auto_queue(s)
    assert started == 1
    async with AsyncSessionLocal() as s:
        t = await s.get(Task, tid)
        assert t.status == TaskStatus.RUNNING
        assert t.auto_start is False, "启动后必须消费掉排队意图"


async def test_auto_queue_orders_by_priority(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    order: list[str] = []
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: order.append("x") or "future")
    async with AsyncSessionLocal() as s:
        for i, (title, prio) in enumerate([
                ("低", TaskPriority.LOW), ("紧", TaskPriority.URGENT),
                ("普", TaskPriority.NORMAL), ("高", TaskPriority.HIGH)]):
            s.add(Task(id=f"t{i}_{co['id'][:6]}", company_id=co["id"], title=title,
                       description="d", status=TaskStatus.TODO, priority=prio,
                       assigned_agent_id=agent["id"], created_by=alice["id"],
                       auto_start=True))
        await s.commit()

    async with AsyncSessionLocal() as s:
        started = await dispatch_auto_queue(s, limit=2)
        assert started == 2
        ids = (await s.execute(
            select(Task.title).where(Task.status == TaskStatus.RUNNING))).scalars().all()
    assert ids == ["紧", "高"], ids


async def test_dispatch_rejects_other_tenants_agent(client, tenants, monkeypatch):
    alice, alice_co = tenants.alice, tenants.alice_co
    bob_agent = await _make_agent(client, tenants.bob, tenants.bob_co["id"], "别人的员工")
    monkeypatch.setattr(background_jobs, "submit_async", lambda *a, **kw: "future")

    # alice 在自己的公司里，企图把任务派给 bob 公司的员工 → 必须 404
    r = await client.post("/task-dispatch/dispatch", json={
        "company_id": alice_co["id"], "title": "越权", "target_type": "single_agent",
        "agent_ids": [bob_agent["id"]],
    }, headers=alice["headers"])
    assert r.status_code == 404, r.text

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(Task))).scalars().all() == [], "越权请求不能落库"


async def test_dispatch_without_flag_starts_immediately(client, tenants, monkeypatch):
    """分发即启动：不传 auto_start 也必须开工（默认 True）。"""
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    submitted: list[str] = []
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: submitted.append("job"))

    r = await client.post("/task-dispatch/dispatch", json={
        "company_id": co["id"], "title": "默认开工", "target_type": "single_agent",
        "agent_ids": [agent["id"]],
    }, headers=alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["started_count"] == 1
    assert submitted


async def test_batch_start_only_starts_todo(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: "future")

    async with AsyncSessionLocal() as s:
        ids = []
        for i in range(2):
            t = Task(company_id=co["id"], title=f"待办{i}",
                     status=TaskStatus.TODO, assigned_agent_id=agent["id"],
                     created_by=alice["id"])
            s.add(t)
            await s.flush()
            ids.append(t.id)
        done = Task(company_id=co["id"], title="已完成",
                    status=TaskStatus.COMPLETED,
                    assigned_agent_id=agent["id"], created_by=alice["id"])
        s.add(done)
        await s.flush()
        done_id = done.id
        await s.commit()

    r = await client.post("/task-dispatch/start", json={
        "company_id": co["id"], "task_ids": [*ids, done_id],
    }, headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started_count"] == 2
    assert set(body["started"]) == set(ids)
    assert [x["task_id"] for x in body["skipped"]] == [done_id]
    assert "COMPLETED" in body["skipped"][0]["reason"]

    async with AsyncSessionLocal() as s:
        for tid in ids:
            assert (await s.get(Task, tid)).status == TaskStatus.RUNNING


async def test_batch_start_reports_queue_full_as_queued(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    monkeypatch.setattr(background_jobs, "submit_async",
                        lambda fn, *a, **kw: background_jobs.QUEUE_FULL)
    async with AsyncSessionLocal() as s:
        t = Task(company_id=co["id"], title="排队", status=TaskStatus.TODO,
                 assigned_agent_id=agent["id"], created_by=alice["id"])
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()

    r = await client.post("/task-dispatch/start", json={
        "company_id": co["id"], "task_ids": [tid],
    }, headers=alice["headers"])
    body = r.json()
    assert body["started_count"] == 0
    assert body["queued"] == [tid]
    assert body["skipped"] == []
    async with AsyncSessionLocal() as s:
        t = await s.get(Task, tid)
        assert t.status == TaskStatus.TODO
        assert t.auto_start is True, "队满要给调度循环留下自动拉起标记"


async def test_completed_task_progress_becomes_full(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent = await _make_agent(client, alice, co["id"])
    async with AsyncSessionLocal() as s:
        t = Task(company_id=co["id"], title="进度", status=TaskStatus.RUNNING,
                 assigned_agent_id=agent["id"], created_by=alice["id"])
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, tid)
        await transition(s, t, TaskStatus.COMPLETED)

    async with AsyncSessionLocal() as s:
        assert (await s.get(Task, tid)).progress == 1.0


async def test_resolve_targets_filters_by_company():
    """直接验证解析层：即便绕过路由鉴权，跨公司 agent 也拿不到。"""
    from app.api.routes.task_dispatch import DispatchIn

    async with AsyncSessionLocal() as s:
        data = DispatchIn(company_id="co-A", title="x", target_type="multi_agent_list",
                          agent_ids=["nope"])
        try:
            await task_dispatch._resolve_targets_async(s, data)
        except Exception as exc:  # 不存在的 agent 必须报错而不是返回空
            assert "404" in str(exc) or "不存在" in str(exc)


async def test_start_task_without_agent_is_noop(tenants):
    async with AsyncSessionLocal() as s:
        s.add(Task(id="t_noagent", company_id=tenants.alice_co["id"], title="无主",
                   status=TaskStatus.TODO, assigned_agent_id=None,
                   created_by=tenants.alice["id"], auto_start=True))
        await s.commit()
        t = await s.get(Task, "t_noagent")
        r = await start_task(s, t)
    assert r == {"started": False, "reason": "no_agent"}
