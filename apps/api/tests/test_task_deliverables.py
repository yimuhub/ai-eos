"""任务完成 → 交付单 → 收件箱「待验收」：验收闭环不再断在 tasks.result。

回归对象：任务跑完只写 tasks.result，收件箱里永远只有"已完成"；
IN_REVIEW → APPROVED → accepted_at 的验收链路无人触发。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.task import Task, TaskStatus
from app.services.deliverables import record_task_deliverable
from app.task_engine import transition

pytestmark = pytest.mark.asyncio

CONTENT = "发布会主文案：\n1. 开场……\n2. 痛点……"


async def _make_agent(client, user, company_id: str, name: str = "文案写手") -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "文案"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_running_task(company_id: str, user_id: str, agent_id: str,
                             *, result: str | None) -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title="写一份发布会主文案",
                 description="", status=TaskStatus.RUNNING,
                 created_by=user_id, assigned_agent_id=agent_id)
        t.result = result
        s.add(t)
        await s.flush()
        task_id = t.id
        await s.commit()
    return task_id


async def _artifact_for(task_id: str) -> Artifact:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Artifact).where(Artifact.task_id == task_id))).scalar_one()


async def test_completed_task_with_output_lands_in_awaiting_acceptance(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_id = await _make_agent(client, alice, co["id"])
    task_id = await _seed_running_task(co["id"], alice["id"], agent_id,
                                       result=CONTENT)

    async with AsyncSessionLocal() as s:
        await transition(s, await s.get(Task, task_id), TaskStatus.COMPLETED)

    art = await _artifact_for(task_id)
    assert art.status == ArtifactStatus.IN_REVIEW
    assert art.current_version == 1
    assert art.created_by == alice["id"], "created_by 是用户 id（验收通知按它发）"
    async with AsyncSessionLocal() as s:
        ver = (await s.execute(select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == art.id))).scalar_one()
    assert ver.version == 1
    assert ver.content == CONTENT

    r = await client.get(f"/tasks/inbox?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["awaiting_acceptance"] == 1
    assert body["counts"]["completed"] == 0
    item = body["awaiting_acceptance"][0]
    assert item["id"] == task_id
    assert item["artifact_id"] == art.id
    assert item["artifact_status"] == "IN_REVIEW"


async def test_completed_task_without_output_stays_completed(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_id = await _make_agent(client, alice, co["id"])
    task_id = await _seed_running_task(co["id"], alice["id"], agent_id,
                                       result=None)

    async with AsyncSessionLocal() as s:
        await transition(s, await s.get(Task, task_id), TaskStatus.COMPLETED)

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(Artifact).where(
            Artifact.task_id == task_id))).scalar_one_or_none() is None

    r = await client.get(f"/tasks/inbox?company_id={co['id']}",
                         headers=alice["headers"])
    body = r.json()
    assert body["counts"]["awaiting_acceptance"] == 0
    assert body["counts"]["completed"] == 1


async def test_deliverable_is_idempotent_per_task(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_id = await _make_agent(client, alice, co["id"])
    task_id = await _seed_running_task(co["id"], alice["id"], agent_id,
                                       result=CONTENT)

    async with AsyncSessionLocal() as s:
        task = await s.get(Task, task_id)
        assert await record_task_deliverable(s, task) is not None
        assert await record_task_deliverable(s, task) is None
        await s.commit()

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Artifact).where(
            Artifact.task_id == task_id))).scalars().all()
    assert len(rows) == 1, "同一任务只允许一张交付单"


async def test_accepting_deliverable_moves_task_to_completed(client, tenants):
    """审核通过 + 验收后，任务从「待验收」落到「已完成」。"""
    alice, co = tenants.alice, tenants.alice_co
    agent_id = await _make_agent(client, alice, co["id"])
    task_id = await _seed_running_task(co["id"], alice["id"], agent_id,
                                       result=CONTENT)
    async with AsyncSessionLocal() as s:
        await transition(s, await s.get(Task, task_id), TaskStatus.COMPLETED)
    art = await _artifact_for(task_id)

    r = await client.post(f"/artifacts/{art.id}/review",
                          json={"decision": "approve", "comment": ""},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/artifacts/{art.id}/accept",
                          headers=alice["headers"])
    assert r.status_code == 200, r.text

    body = (await client.get(f"/tasks/inbox?company_id={co['id']}",
                             headers=alice["headers"])).json()
    assert body["counts"]["awaiting_acceptance"] == 0
    assert body["counts"]["completed"] == 1
    assert body["completed"][0]["artifact_status"] == "APPROVED"
    assert body["completed"][0]["artifact_id"] == art.id
