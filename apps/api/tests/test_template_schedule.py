"""B4：模板 → 定时任务（create_task_from_template 调度动作）。

覆盖：
- 保存的模板 + 调度 tick → 生成 TODO 任务（标题/优先级/描述/创建人），
  并写入 CREATED 任务事件与 task.created_from_template 事件；
- 跨租户：A 公司的调度引用 B 公司的模板 → 运行失败且不建任务；
- 缺失 template_id / 非模板定义 → 失败且不建任务；
- 同公司指派员工保留、外公司指派员工清空、非法优先级降级 NORMAL；
- interval / daily 入参校验保持原样（未因新动作放宽）。

全部走 conftest 的临时 SQLite + 进程内 ASGI 客户端，不触碰真实库。
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.automation import AutomationDefinition
from app.models.event import EventLog
from app.models.scheduler import AutomationRun, AutomationSchedule
from app.models.task import Task, TaskEvent, TaskEventType, TaskPriority, TaskStatus


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_completed_task(company_id: str, user_id: str,
                               title: str, priority=TaskPriority.NORMAL,
                               description: str | None = "模板说明") -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title=title, description=description,
                 priority=priority, status=TaskStatus.COMPLETED,
                 created_by=user_id)
        s.add(t)
        await s.commit()
        await s.refresh(t)
        return t.id


async def _save_template(client, tenant, title: str, **kwargs) -> str:
    tid = await _make_completed_task(tenant["co"], tenant["user"], title,
                                     **kwargs)
    r = await client.post(f"/tasks/{tid}/save-as-template",
                          headers=tenant["headers"])
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _create_schedule(client, tenant, params: dict,
                           trigger_type: str = "interval",
                           seconds: int | None = 60) -> dict:
    body = {"name": f"定时-{uuid.uuid4().hex[:6]}",
            "trigger_type": trigger_type,
            "action_type": "create_task_from_template",
            "action_params": params}
    if seconds is not None:
        body["seconds"] = seconds
    r = await client.post(f"/schedules?company_id={tenant['co']}",
                          json=body, headers=tenant["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _make_due(sid: str) -> None:
    async with AsyncSessionLocal() as s:
        sch = await s.get(AutomationSchedule, sid)
        sch.next_run_at = _utcnow() - timedelta(seconds=5)
        await s.commit()


async def _tick(client, tenant) -> dict:
    r = await client.post(f"/schedules/tick?company_id={tenant['co']}",
                          headers=tenant["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _tenant(user: dict, co: dict) -> dict:
    return {"user": user["id"], "co": co["id"], "headers": user["headers"]}


async def _tasks_with_title(company_id: str, title: str) -> list[Task]:
    """按标题找**调度新建**的待办任务（模板源任务是 COMPLETED，不计入）。"""
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(Task).where(
            Task.company_id == company_id, Task.title == title,
            Task.status == TaskStatus.TODO))).scalars().all()


async def test_template_schedule_creates_todo_task(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    title = f"周报模板-{uuid.uuid4().hex[:6]}"
    template_id = await _save_template(client, alice, title,
                                       priority=TaskPriority.HIGH)

    sched = await _create_schedule(client, alice, {"template_id": template_id})
    assert sched["action_type"] == "create_task_from_template"
    assert sched["action_params"] == {"template_id": template_id}
    await _make_due(sched["id"])

    assert (await _tick(client, alice))["executed"] == 1

    tasks = await _tasks_with_title(alice["co"], title)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.status == TaskStatus.TODO
    assert t.priority == TaskPriority.HIGH
    assert t.description == "模板说明"
    assert t.created_by == alice["user"]

    async with AsyncSessionLocal() as s:
        ev = (await s.execute(select(TaskEvent).where(
            TaskEvent.task_id == t.id,
            TaskEvent.event_type == TaskEventType.CREATED))).scalars().all()
        assert len(ev) == 1
        assert ev[0].actor_type == "system"
        assert ev[0].payload["template_id"] == template_id

        logs = (await s.execute(select(EventLog).where(
            EventLog.event_type == "task.created_from_template",
            EventLog.entity_id == t.id))).scalars().all()
        assert len(logs) == 1
        assert logs[0].company_id == alice["co"]

        run = (await s.execute(select(AutomationRun).where(
            AutomationRun.schedule_id == sched["id"]))).scalars().one()
        assert run.status == "SUCCESS"
        assert run.result == {"task_id": t.id, "template_id": template_id}


async def test_cross_tenant_template_is_rejected(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    bob = _tenant(tenants.bob, tenants.bob_co)
    title = f"B公司模板-{uuid.uuid4().hex[:6]}"
    bob_template = await _save_template(client, bob, title)

    # A 公司可以创建调度（创建时不校验模板），执行时必须失败
    sched = await _create_schedule(client, alice,
                                   {"template_id": bob_template})
    await _make_due(sched["id"])
    assert (await _tick(client, alice))["executed"] == 1

    assert await _tasks_with_title(alice["co"], title) == []
    async with AsyncSessionLocal() as s:
        run = (await s.execute(select(AutomationRun).where(
            AutomationRun.schedule_id == sched["id"]))).scalars().one()
    assert run.status == "FAILED"
    assert "not found" in (run.error or "")


async def test_missing_template_id_fails_without_task(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    sched = await _create_schedule(client, alice, {})
    await _make_due(sched["id"])
    await _tick(client, alice)

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(Task).where(
            Task.company_id == alice["co"]))).scalars().all() == []
        run = (await s.execute(select(AutomationRun).where(
            AutomationRun.schedule_id == sched["id"]))).scalars().one()
    assert run.status == "FAILED"
    assert "template_id required" in (run.error or "")


async def test_non_template_definition_is_rejected(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    async with AsyncSessionLocal() as s:
        d = AutomationDefinition(
            id=str(uuid.uuid4()), company_id=alice["co"], name="普通规则",
            trigger_type="MANUAL", trigger_config={}, mode="TEST",
            status="PAUSED", created_by=alice["user"],
            steps=[{"seq": 1, "type": "create_task",
                    "params": {"title": "不应该被创建"}}])
        s.add(d)
        await s.commit()

    sched = await _create_schedule(client, alice, {"template_id": d.id})
    await _make_due(sched["id"])
    await _tick(client, alice)

    assert await _tasks_with_title(alice["co"], "不应该被创建") == []
    async with AsyncSessionLocal() as s:
        run = (await s.execute(select(AutomationRun).where(
            AutomationRun.schedule_id == sched["id"]))).scalars().one()
    assert run.status == "FAILED"


async def test_bad_priority_and_foreign_agent_are_sanitized(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    bob = _tenant(tenants.bob, tenants.bob_co)
    title = f"脏模板-{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as s:
        foreign_agent = Agent(company_id=bob["co"], name="别人员工")
        s.add(foreign_agent)
        d = AutomationDefinition(
            id=str(uuid.uuid4()), company_id=alice["co"], name=title,
            trigger_type="MANUAL",
            trigger_config={"kind": "TASK_TEMPLATE"}, mode="TEST",
            status="PAUSED", created_by=alice["user"],
            steps=[{"seq": 1, "type": "create_task", "params": {
                "title": title, "priority": "SUPER_HIGH",
                "assigned_agent_id": foreign_agent.id}}])
        s.add(d)
        await s.commit()
        template_id, foreign_agent_id = d.id, foreign_agent.id

        # 无创建人的调度：任务创建人回退为公司 owner
        sch = AutomationSchedule(
            id=str(uuid.uuid4()), company_id=alice["co"], name="无创建人调度",
            trigger_type="interval", trigger_config={"seconds": 60},
            action_type="create_task_from_template",
            action_params={"template_id": template_id},
            created_by=None, next_run_at=_utcnow() - timedelta(seconds=5))
        s.add(sch)
        await s.commit()
        sid = sch.id

    await _tick(client, alice)

    tasks = await _tasks_with_title(alice["co"], title)
    assert len(tasks) == 1
    assert tasks[0].priority == TaskPriority.NORMAL
    assert tasks[0].assigned_agent_id is None
    assert tasks[0].created_by == alice["user"]
    async with AsyncSessionLocal() as s:
        run = (await s.execute(select(AutomationRun).where(
            AutomationRun.schedule_id == sid))).scalars().one()
    assert run.status == "SUCCESS"
    # 外公司员工当然不会被改写：只验证任务里没有挂上
    async with AsyncSessionLocal() as s:
        assert (await s.get(Agent, foreign_agent_id)).company_id == bob["co"]


async def test_same_company_agent_is_kept(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    title = f"带员工模板-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as s:
        agent = Agent(company_id=alice["co"], name="本公司员工")
        s.add(agent)
        await s.commit()
        agent_id = agent.id

    tid = await _make_completed_task(alice["co"], alice["user"], title)
    async with AsyncSessionLocal() as s:
        # 直接把指派员工写进模板步骤（save-as-template 只复制源任务字段）
        t = await s.get(Task, tid)
        t.assigned_agent_id = agent_id
        await s.commit()
    r = await client.post(f"/tasks/{tid}/save-as-template",
                          headers=alice["headers"])
    template_id = r.json()["id"]

    sched = await _create_schedule(client, alice, {"template_id": template_id})
    await _make_due(sched["id"])
    await _tick(client, alice)

    tasks = await _tasks_with_title(alice["co"], title)
    assert len(tasks) == 1
    assert tasks[0].assigned_agent_id == agent_id


async def test_schedule_trigger_validation_unchanged(client, tenants):
    alice = _tenant(tenants.alice, tenants.alice_co)
    url = f"/schedules?company_id={alice['co']}"
    base = {"name": "校验", "action_type": "create_task_from_template",
            "action_params": {}}

    r = await client.post(url, json={**base, "trigger_type": "interval",
                                     "seconds": 9},
                          headers=alice["headers"])
    assert r.status_code == 400
    assert "seconds >= 10" in r.json()["detail"]

    r = await client.post(url, json={**base, "trigger_type": "cron"},
                          headers=alice["headers"])
    assert r.status_code == 400

    r = await client.post(url, json={**base, "trigger_type": "daily"},
                          headers=alice["headers"])
    assert r.status_code == 400

    r = await client.post(url, json={**base, "trigger_type": "interval",
                                     "seconds": 120},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trigger_config"] == {"seconds": 120}
    assert body["next_run_at"] is not None

    r = await client.post(url, json={**base, "trigger_type": "daily",
                                     "time": "09:30"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["trigger_config"] == {"time": "09:30"}

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(Task).where(
            Task.company_id == alice["co"]))).scalars().all() == []
