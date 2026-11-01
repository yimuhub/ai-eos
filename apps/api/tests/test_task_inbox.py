"""C1 任务收件箱：分桶口径、成果验收、租户隔离与空公司。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact, ArtifactStatus
from app.models.task import Task, TaskPriority, TaskStatus

pytestmark = pytest.mark.asyncio

BASE = datetime.now(timezone.utc).replace(tzinfo=None)


def _at(minutes: int) -> datetime:
    return BASE - timedelta(minutes=minutes)


async def _make_agent(client, user, company_id: str, name: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": "测试"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_task(company_id: str, user_id: str, *, title: str,
                     status: TaskStatus, minutes: int,
                     agent_id: str | None = None) -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title=title, status=status,
                 priority=TaskPriority.NORMAL, created_by=user_id,
                 assigned_agent_id=agent_id,
                 created_at=_at(minutes), updated_at=_at(minutes))
        s.add(t)
        await s.flush()
        task_id = t.id
        await s.commit()
    return task_id


async def _seed_artifact(company_id: str, task_id: str, *,
                         status: ArtifactStatus, accepted: bool = False,
                         minutes: int = 1) -> str:
    async with AsyncSessionLocal() as s:
        a = Artifact(id=f"art-{task_id}", company_id=company_id,
                     name=f"成果-{task_id[:6]}", artifact_type="document",
                     current_version=1, status=status, task_id=task_id,
                     accepted_at=_at(minutes) if accepted else None,
                     created_at=_at(minutes))
        s.add(a)
        await s.commit()
    return a.id


async def test_inbox_buckets_by_status_and_artifact_acceptance(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    agent_id = await _make_agent(client, alice, co["id"], "张三")

    running = await _seed_task(co["id"], alice["id"], title="正在跑",
                               status=TaskStatus.RUNNING, minutes=1,
                               agent_id=agent_id)
    awaiting = await _seed_task(co["id"], alice["id"], title="等验收",
                                status=TaskStatus.COMPLETED, minutes=2)
    accepted = await _seed_task(co["id"], alice["id"], title="已验收",
                                status=TaskStatus.COMPLETED, minutes=3)
    no_artifact = await _seed_task(co["id"], alice["id"], title="无成果完成",
                                   status=TaskStatus.COMPLETED, minutes=4)
    failed = await _seed_task(co["id"], alice["id"], title="失败",
                              status=TaskStatus.FAILED, minutes=5)
    cancelled = await _seed_task(co["id"], alice["id"], title="已取消",
                                 status=TaskStatus.CANCELLED, minutes=6)

    await _seed_artifact(co["id"], awaiting, status=ArtifactStatus.IN_REVIEW)
    await _seed_artifact(co["id"], accepted, status=ArtifactStatus.APPROVED,
                         accepted=True)

    r = await client.get(f"/tasks/inbox?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["counts"] == {"in_progress": 1, "awaiting_acceptance": 1,
                              "completed": 2, "failed": 1}
    assert [i["id"] for i in body["in_progress"]] == [running]
    assert [i["id"] for i in body["awaiting_acceptance"]] == [awaiting]
    assert {i["id"] for i in body["completed"]} == {accepted, no_artifact}
    assert [i["id"] for i in body["failed"]] == [failed]

    all_ids = {i["id"] for bucket in
               ("in_progress", "awaiting_acceptance", "completed", "failed")
               for i in body[bucket]}
    assert cancelled not in all_ids, "主动取消不算失败，也不该出现在其它桶"

    item = body["in_progress"][0]
    assert item["title"] == "正在跑"
    assert item["status"] == "RUNNING"
    assert item["agent_id"] == agent_id
    assert item["agent_name"] == "张三"
    assert item["updated_at"] is not None

    awaiting_item = body["awaiting_acceptance"][0]
    assert awaiting_item["artifact_id"] is not None
    assert awaiting_item["artifact_status"] == "IN_REVIEW"

    # 无成果的已完成任务：成果字段为空，而不是编造一个
    done = {i["id"]: i for i in body["completed"]}
    assert done[no_artifact]["artifact_id"] is None
    assert done[no_artifact]["artifact_status"] is None
    assert done[accepted]["artifact_status"] == "APPROVED"


async def test_inbox_rejects_foreign_company(client, tenants):
    r = await client.get(f"/tasks/inbox?company_id={tenants.alice_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, "外部租户读别人公司的收件箱必须 404"

    r = await client.get("/tasks/inbox?company_id=does-not-exist",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_inbox_is_scoped_to_own_company(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _seed_task(co["id"], alice["id"], title="A 公司的活",
                     status=TaskStatus.RUNNING, minutes=1)

    r = await client.get(f"/tasks/inbox?company_id={tenants.bob_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"in_progress": 0, "awaiting_acceptance": 0,
                              "completed": 0, "failed": 0}
    for bucket in ("in_progress", "awaiting_acceptance", "completed", "failed"):
        assert body[bucket] == []


async def test_inbox_orders_newest_first(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    older = await _seed_task(co["id"], alice["id"], title="旧任务",
                             status=TaskStatus.RUNNING, minutes=30)
    newer = await _seed_task(co["id"], alice["id"], title="新任务",
                             status=TaskStatus.RUNNING, minutes=1)

    r = await client.get(f"/tasks/inbox?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["in_progress"]] == [newer, older]
