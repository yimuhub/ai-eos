"""B4：自动化「复制」（clone）——只复制定义，不复制运行记录。

覆盖：
- 属主克隆成功：新 id、名称带「副本」、mode 强制 TEST、status 初始 PAUSED；
- 原定义的运行记录不会被复制到副本；
- 跨租户 clone 404，且不产生任何新定义；
- 原定义为 PRODUCTION/ENABLED 时，副本仍降级为 TEST/PAUSED。
"""
import uuid

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.automation import AutomationDefinition, AutomationRunRecord


async def _create(client, tenants, *, mode: str = "TEST", steps=None) -> str:
    r = await client.post(
        f"/automations?company_id={tenants.alice_co['id']}",
        json={"name": f"自动化-{uuid.uuid4().hex[:6]}",
              "trigger_type": "MANUAL",
              "steps": steps or [{"seq": 1, "type": "noop", "params": {}}],
              "mode": mode},
        headers=tenants.alice["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _runs(definition_id: str) -> int:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AutomationRunRecord).where(
            AutomationRunRecord.definition_id == definition_id))).scalars().all()
        return len(rows)


async def test_owner_clones_definition_without_runs(client, tenants):
    src_id = await _create(client, tenants)
    run = await client.post(f"/automations/{src_id}/run",
                            headers=tenants.alice["headers"])
    assert run.status_code == 200, run.text
    assert await _runs(src_id) == 1

    r = await client.post(f"/automations/{src_id}/clone",
                          headers=tenants.alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] and body["id"] != src_id
    assert body["name"].endswith("（副本）")
    assert body["mode"] == "TEST"
    assert body["status"] == "PAUSED"
    assert body["steps_count"] == 1

    async with AsyncSessionLocal() as s:
        src = await s.get(AutomationDefinition, src_id)
        clone = await s.get(AutomationDefinition, body["id"])
    assert clone.company_id == src.company_id
    assert clone.steps == src.steps
    assert clone.trigger_type == src.trigger_type
    assert clone.created_by == tenants.alice["id"]
    # 副本不携带运行记录；原定义的记录保持不变
    assert await _runs(body["id"]) == 0
    assert await _runs(src_id) == 1


async def test_clone_is_tenant_scoped(client, tenants):
    src_id = await _create(client, tenants)

    r = await client.post(f"/automations/{src_id}/clone",
                          headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AutomationDefinition).where(
            AutomationDefinition.company_id == tenants.alice_co["id"]))).scalars().all()
    assert len(rows) == 1, "越权 clone 不应产生新定义"


async def test_clone_forces_test_and_paused_from_production(client, tenants):
    src_id = await _create(client, tenants, mode="PRODUCTION")
    r = await client.post(f"/automations/{src_id}/enable",
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/automations/{src_id}/clone",
                          headers=tenants.alice["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "TEST"
    assert body["status"] == "PAUSED"
    async with AsyncSessionLocal() as s:
        d = await s.get(AutomationDefinition, body["id"])
    assert d.mode == "TEST" and d.status == "PAUSED"
