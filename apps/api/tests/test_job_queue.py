"""A5 持久队列：幂等入队 / 领取租约 / 重试与死信 / 取消 / 统计 / 租户隔离。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.db.session import AsyncSessionLocal
from app.models.queue_item import QueueItem, QueueItemStatus
from app.services import job_queue

pytestmark = pytest.mark.asyncio


def _past(seconds: int = 5) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


async def test_enqueue_is_idempotent_for_active_dedupe_key():
    async with AsyncSessionLocal() as db:
        first = await job_queue.enqueue(
            db, kind="demo.job", payload={"n": 1}, company_id="co-1",
            dedupe_key="demo:1")
        await db.commit()
        second = await job_queue.enqueue(
            db, kind="demo.job", payload={"n": 2}, company_id="co-1",
            dedupe_key="demo:1")
        await db.commit()

        assert second.id == first.id, "活跃条目被重复插入，幂等键失效"
        assert second.payload == {"n": 1}, "重复入队不应覆盖已有条目"
        n = (await db.execute(select(func.count(QueueItem.id)))).scalar_one()
        assert n == 1
        assert second.status == QueueItemStatus.QUEUED
        assert second.attempts == 0


async def test_dedupe_key_frees_up_after_terminal_state():
    async with AsyncSessionLocal() as db:
        first = await job_queue.enqueue(
            db, kind="demo.job", payload={}, company_id="co-1", dedupe_key="demo:2")
        await db.commit()
        await job_queue.complete(db, first.id)

        again = await job_queue.enqueue(
            db, kind="demo.job", payload={}, company_id="co-1", dedupe_key="demo:2")
        await db.commit()
        assert again.id != first.id, "条目结束后同键应能重新入队"


async def test_claim_sets_running_lease_and_blocks_second_claim():
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1")
        await db.commit()

        claimed = await job_queue.claim_next(db, "worker-a", lease_s=60)
        assert claimed is not None and claimed.id == item.id
        assert claimed.status == QueueItemStatus.RUNNING
        assert claimed.claimed_by == "worker-a"
        assert claimed.attempts == 1
        assert claimed.lease_expires_at is not None

        # 竞争对手拿不到同一条；显式 claim_item 也抢不到（租约仍有效）
        assert await job_queue.claim_next(db, "worker-b", lease_s=60) is None
        assert await job_queue.claim_item(db, item.id, "worker-b") is None


async def test_expired_lease_is_recovered_and_claimable_again():
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1")
        await db.commit()
        await job_queue.claim_next(db, "worker-a", lease_s=60)
        # 直接改租约为过去：模拟持有者进程崩溃
        await db.execute(update(QueueItem).where(QueueItem.id == item.id)
                         .values(lease_expires_at=_past(30))
                         .execution_options(synchronize_session=False))
        await db.commit()

        assert await job_queue.recover_expired(db) == 1
        row = await db.get(QueueItem, item.id, populate_existing=True)
        assert row.status == QueueItemStatus.QUEUED
        assert row.claimed_by is None
        assert row.lease_expires_at is None

        reclaimed = await job_queue.claim_item(db, item.id, "worker-b")
        assert reclaimed is not None, "回收后的条目应能被重新领取"
        assert reclaimed.attempts == 2, "每次领取都要计入 attempts（用于死信判定）"


async def test_fail_requeues_with_backoff_then_goes_dead():
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1", max_attempts=2)
        await db.commit()

        claimed = await job_queue.claim_next(db, "worker-a", lease_s=60)
        assert claimed.attempts == 1
        requeued = await job_queue.fail(db, item.id, "boom", retry_backoff_s=0)
        assert requeued.status == QueueItemStatus.QUEUED
        assert requeued.last_error == "boom"
        assert requeued.available_at <= datetime.now(timezone.utc).replace(tzinfo=None)

        claimed = await job_queue.claim_next(db, "worker-a", lease_s=60)
        assert claimed.attempts == 2
        dead = await job_queue.fail(db, item.id, "boom again", retry_backoff_s=0)
        assert dead.status == QueueItemStatus.DEAD
        assert dead.last_error == "boom again"
        assert await job_queue.claim_next(db, "worker-a") is None


async def test_fail_respects_backoff_before_item_becomes_available():
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="demo.job", payload={},
                                       company_id="co-1", max_attempts=2)
        await db.commit()
        await job_queue.claim_next(db, "worker-a")
        await job_queue.fail(db, item.id, "slow down", retry_backoff_s=120)
        # 退避期内不应对任何 worker 可见
        assert await job_queue.claim_next(db, "worker-b") is None


async def test_cancel_only_affects_queued_items():
    async with AsyncSessionLocal() as db:
        queued = await job_queue.enqueue(db, kind="demo.job", payload={},
                                         company_id="co-1")
        running = await job_queue.enqueue(db, kind="demo.job", payload={},
                                          company_id="co-1")
        await db.commit()
        await job_queue.claim_item(db, running.id, "worker-a")

        assert await job_queue.cancel(db, queued.id) is True
        assert (await db.get(QueueItem, queued.id, populate_existing=True)).status \
            == QueueItemStatus.CANCELLED
        assert await job_queue.cancel(db, running.id) is False
        assert (await db.get(QueueItem, running.id, populate_existing=True)).status \
            == QueueItemStatus.RUNNING


async def test_cancel_active_by_dedupe_leaves_running_untouched():
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(db, kind="task.execute", payload={},
                                       company_id="co-1", dedupe_key="task:t-1")
        await db.commit()
        assert await job_queue.cancel_active(
            db, kind="task.execute", dedupe_key="task:t-1", company_id="co-1") == 1
        assert (await db.get(QueueItem, item.id, populate_existing=True)).status \
            == QueueItemStatus.CANCELLED


async def test_stats_counts_by_status_and_oldest_wait():
    async with AsyncSessionLocal() as db:
        q1 = await job_queue.enqueue(db, kind="demo.job", payload={},
                                     company_id="co-1")
        q2 = await job_queue.enqueue(db, kind="demo.job", payload={},
                                     company_id="co-2")
        await db.commit()
        await job_queue.claim_item(db, q2.id, "worker-a")
        await db.execute(update(QueueItem).where(QueueItem.id == q1.id)
                         .values(created_at=_past(90))
                         .execution_options(synchronize_session=False))
        await db.commit()

        all_stats = await job_queue.stats(db)
        assert all_stats["queued"] == 1
        assert all_stats["running"] == 1
        assert all_stats["total"] == 2
        assert all_stats["oldest_queued_seconds"] >= 80

        co1 = await job_queue.stats(db, "co-1")
        assert co1["queued"] == 1 and co1["running"] == 0 and co1["total"] == 1


# ---------------------------------------------------------------------------
# HTTP：列表 / 统计 / 取消 的租户隔离
# ---------------------------------------------------------------------------

async def _seed_item(company_id: str, *, dedupe_key: str | None = None) -> str:
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(
            db, kind="task.execute", payload={"task_id": "t-secret", "api_key": "sk-leak"},
            company_id=company_id, dedupe_key=dedupe_key)
        await db.commit()
        return item.id


async def test_queue_list_is_tenant_scoped_and_hides_payload(client, tenants):
    item_id = await _seed_item(tenants.alice_co["id"])

    r = await client.get(f"/queue?company_id={tenants.alice_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [item_id]
    row = rows[0]
    assert row["status"] == "QUEUED"
    assert row["kind"] == "task.execute"
    assert row["attempts"] == 0 and row["max_attempts"] == 3
    for field in ("available_at", "created_at", "last_error"):
        assert field in row
    assert "payload" not in row, "队列列表不应回传 payload（可能含密钥/业务正文）"
    assert "sk-leak" not in r.text

    # 外部租户：拿 alice 的公司 id 取列表 → 404（不泄露公司是否存在）
    r = await client.get(f"/queue?company_id={tenants.alice_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404

    # bob 自己的公司：看不到 alice 的条目
    r = await client.get(f"/queue?company_id={tenants.bob_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 200
    assert r.json() == []


async def test_queue_stats_endpoint_is_tenant_scoped(client, tenants):
    await _seed_item(tenants.alice_co["id"])

    r = await client.get(f"/queue/stats?company_id={tenants.alice_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["company_id"] == tenants.alice_co["id"]
    assert body["queued"] == 1 and body["total"] == 1
    assert "oldest_queued_seconds" in body

    r = await client.get(f"/queue/stats?company_id={tenants.alice_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_queue_cancel_endpoint_checks_tenant_and_state(client, tenants):
    queued_id = await _seed_item(tenants.alice_co["id"])

    # bob 拿着 alice 的条目 id：404
    r = await client.post(f"/queue/{queued_id}/cancel", headers=tenants.bob["headers"])
    assert r.status_code == 404

    r = await client.post(f"/queue/{queued_id}/cancel", headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED"

    # 已结束的条目再取消 → 409
    r = await client.post(f"/queue/{queued_id}/cancel", headers=tenants.alice["headers"])
    assert r.status_code == 409

    # 执行中的条目 → 409（不能强杀）
    running_id = await _seed_item(tenants.alice_co["id"], dedupe_key="run:1")
    async with AsyncSessionLocal() as db:
        await job_queue.claim_item(db, running_id, "worker-x")
    r = await client.post(f"/queue/{running_id}/cancel", headers=tenants.alice["headers"])
    assert r.status_code == 409, r.text


async def test_queue_cancel_unknown_item_is_404(client, make_user):
    user = await make_user()
    r = await client.post("/queue/does-not-exist/cancel", headers=user["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 主链路：启动任务必须留下队列行，并由执行结果收尾
# ---------------------------------------------------------------------------

async def _queue_row(task_id: str) -> QueueItem | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(QueueItem).where(
            QueueItem.dedupe_key == f"task:{task_id}"))).scalars().first()


async def test_task_start_persists_queue_item_and_settles_it(client, make_user,
                                                             make_company):
    import asyncio

    user = await make_user()
    co = await make_company(user)
    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "无模型员工", "role": "测试"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "队列落库任务",
        "assigned_agent_id": agent_id}, headers=user["headers"])
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = await client.post(f"/tasks/{task_id}/start", headers=user["headers"])
    assert r.status_code == 200, r.text

    # 没有可用模型 → 任务必然失败；但队列行必须落库并离开"执行中"
    deadline = asyncio.get_event_loop().time() + 60
    row = None
    while asyncio.get_event_loop().time() < deadline:
        row = await _queue_row(task_id)
        if row is not None and row.status in (QueueItemStatus.DEAD,
                                              QueueItemStatus.SUCCEEDED):
            break
        await asyncio.sleep(0.3)
    assert row is not None, "启动任务没有写入持久队列行"
    assert row.kind == "task.execute"
    assert row.company_id == co["id"]
    assert row.payload == {"task_id": task_id, "agent_id": agent_id}
    assert row.attempts == 1
    assert row.status == QueueItemStatus.DEAD, \
        f"执行失败后条目应进入终态，实际 {row.status}"
    assert row.last_error, "失败条目必须留下原因"

    # 重复点击「开始」不会造出第二条队列行
    r = await client.post(f"/tasks/{task_id}/start", headers=user["headers"])
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count(QueueItem.id)).where(
            QueueItem.dedupe_key == f"task:{task_id}"))).scalar_one()
    assert n == 1


# ---------------------------------------------------------------------------
# 启动自愈：过期租约回收 + 重新投递，且不会重复执行
# ---------------------------------------------------------------------------

async def _seed_task_row(company_id: str, user_id: str, agent_id: str,
                         status) -> str:
    from app.models.task import Task, TaskPriority

    async with AsyncSessionLocal() as db:
        t = Task(company_id=company_id, title="重启前没跑完的任务",
                 status=status, priority=TaskPriority.NORMAL,
                 created_by=user_id, assigned_agent_id=agent_id)
        db.add(t)
        await db.flush()
        task_id = t.id
        await db.commit()
    return task_id


async def _wait_status(item_id: str, wanted: set, timeout: float = 60.0):
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    row = None
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as db:
            row = await db.get(QueueItem, item_id, populate_existing=True)
        if row is not None and row.status in wanted:
            return row
        await asyncio.sleep(0.3)
    raise AssertionError(f"条目 {timeout}s 内未进入 {wanted}，当前 {row.status if row else None}")


async def test_recover_startup_resubmits_and_never_double_runs(client, make_user,
                                                               make_company):
    from app.models.task import TaskStatus

    user = await make_user()
    co = await make_company(user)
    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "自愈员工", "role": "测试"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    task_id = await _seed_task_row(co["id"], user["id"], agent_id,
                                   TaskStatus.RUNNING)
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(
            db, kind="task.execute", company_id=co["id"],
            dedupe_key=f"task:{task_id}",
            payload={"task_id": task_id, "agent_id": agent_id})
        await db.commit()
        item_id = item.id

    first = await job_queue.recover_startup()
    assert first["resubmitted"] == 1, first

    row = await _wait_status(item_id, {QueueItemStatus.DEAD,
                                       QueueItemStatus.SUCCEEDED})
    assert row.attempts == 1, "同一个条目只允许被真正执行一次"

    # 再跑一次自愈：条目已不是 QUEUED，不会再投递
    second = await job_queue.recover_startup()
    assert second["resubmitted"] == 0, second


async def test_recover_startup_cancels_items_for_non_runnable_tasks(
        client, make_user, make_company):
    from app.models.task import TaskStatus

    user = await make_user()
    co = await make_company(user)
    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "自愈员工2", "role": "测试"},
        headers=user["headers"])
    agent_id = r.json()["id"]

    task_id = await _seed_task_row(co["id"], user["id"], agent_id,
                                   TaskStatus.CANCELLED)
    async with AsyncSessionLocal() as db:
        item = await job_queue.enqueue(
            db, kind="task.execute", company_id=co["id"],
            dedupe_key=f"task:{task_id}",
            payload={"task_id": task_id, "agent_id": agent_id})
        await db.commit()
        item_id = item.id

    result = await job_queue.recover_startup()
    assert result["cancelled"] == 1, result
    async with AsyncSessionLocal() as db:
        row = await db.get(QueueItem, item_id, populate_existing=True)
    assert row.status == QueueItemStatus.CANCELLED
