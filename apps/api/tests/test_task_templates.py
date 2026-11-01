"""B4：已完成任务 → 模板（复用 automation_definitions，不造第二套模板表）。

覆盖：
- 属主保存成功，模板为 TEST/PAUSED 且带 create_task 步骤；
- 同名重复保存幂等（返回同一 id、不新增行）；
- 非 COMPLETED 状态 409；
- 跨租户 404；
- 同名普通自动化规则存在时 409（避免模板与规则混淆）。
"""
import uuid

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.automation import AutomationDefinition
from app.models.task import Task, TaskPriority, TaskStatus


async def _make_task(company_id: str, user_id: str,
                     status: TaskStatus = TaskStatus.COMPLETED,
                     title: str | None = None) -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title=title or f"模板源任务-{uuid.uuid4().hex[:6]}",
                 description="把周报整理成一页摘要", priority=TaskPriority.NORMAL,
                 status=status, created_by=user_id)
        s.add(t)
        await s.commit()
        await s.refresh(t)
        return t.id


async def _template_count(company_id: str) -> int:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AutomationDefinition).where(
            AutomationDefinition.company_id == company_id))).scalars().all()
        return sum(1 for d in rows
                   if (d.trigger_config or {}).get("kind") == "TASK_TEMPLATE")


async def test_owner_saves_completed_task_as_template(client, tenants):
    title = f"成功任务-{uuid.uuid4().hex[:6]}"
    tid = await _make_task(tenants.alice_co["id"], tenants.alice["id"], title=title)

    r = await client.post(f"/tasks/{tid}/save-as-template",
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == title
    assert body["id"]

    async with AsyncSessionLocal() as s:
        d = await s.get(AutomationDefinition, body["id"])
    assert d is not None
    assert d.company_id == tenants.alice_co["id"]
    assert d.mode == "TEST"
    assert d.status == "PAUSED"
    assert d.trigger_config["kind"] == "TASK_TEMPLATE"
    assert d.trigger_config["source_task_id"] == tid
    assert d.steps[0]["type"] == "create_task"
    assert d.steps[0]["params"]["title"] == title
    assert d.steps[0]["params"]["description"] == "把周报整理成一页摘要"


async def test_saving_same_name_is_idempotent(client, tenants):
    tid = await _make_task(tenants.alice_co["id"], tenants.alice["id"])
    name = f"复用模板-{uuid.uuid4().hex[:6]}"

    first = await client.post(f"/tasks/{tid}/save-as-template",
                              json={"name": name, "description": "自定义模板说明"},
                              headers=tenants.alice["headers"])
    assert first.status_code == 200, first.text
    second = await client.post(f"/tasks/{tid}/save-as-template",
                               json={"name": name},
                               headers=tenants.alice["headers"])
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["name"] == name
    assert await _template_count(tenants.alice_co["id"]) == 1

    async with AsyncSessionLocal() as s:
        d = await s.get(AutomationDefinition, first.json()["id"])
    assert d.steps[0]["params"]["description"] == "自定义模板说明"


async def test_non_completed_task_is_rejected(client, tenants):
    todo_id = await _make_task(tenants.alice_co["id"], tenants.alice["id"],
                               status=TaskStatus.TODO)
    running_id = await _make_task(tenants.alice_co["id"], tenants.alice["id"],
                                  status=TaskStatus.RUNNING)

    for tid in (todo_id, running_id):
        r = await client.post(f"/tasks/{tid}/save-as-template",
                              headers=tenants.alice["headers"])
        assert r.status_code == 409, r.text
        assert "已完成" in r.json()["detail"]
    assert await _template_count(tenants.alice_co["id"]) == 0


async def test_cross_tenant_cannot_save_template(client, tenants):
    tid = await _make_task(tenants.alice_co["id"], tenants.alice["id"])

    r = await client.post(f"/tasks/{tid}/save-as-template",
                          headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text
    assert await _template_count(tenants.alice_co["id"]) == 0


async def test_same_name_regular_automation_blocks_template(client, tenants):
    """同名但已有普通自动化：409，而不是把规则误当模板返回。"""
    name = f"同名规则-{uuid.uuid4().hex[:6]}"
    r = await client.post(f"/automations?company_id={tenants.alice_co['id']}",
                          json={"name": name, "steps": [{"type": "noop"}]},
                          headers=tenants.alice["headers"])
    assert r.status_code == 201, r.text

    tid = await _make_task(tenants.alice_co["id"], tenants.alice["id"])
    r = await client.post(f"/tasks/{tid}/save-as-template", json={"name": name},
                          headers=tenants.alice["headers"])
    assert r.status_code == 409, r.text
