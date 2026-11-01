"""持久化后台任务队列：入队 / 领取 / 续租 / 完成 / 重试 / 取消 / 回收。

## 为什么还要一层队列

进程内的 ThreadPoolExecutor 一重启就什么都不剩：用户已经点了「开始」，
任务在库里是 RUNNING，但没有任何 worker 在跑它。`queue_items` 把
"已受理、待执行"的意图落库，启动自愈据此重新投递，任务不再静默消失。

## 并发约定（SQLite 与 PostgreSQL 共用一套）

* 领取是条件 UPDATE（`WHERE id=? AND status='QUEUED'`）：读与写之间不加锁，
  rowcount==0 说明被别的 worker 抢走了，换下一条重试；
* 长任务由 `LeaseKeeper` 定期续租。只有租约过期才允许被回收或被抢占，
  因此"另一个 worker 其实还在跑"不会被误判成僵尸；
* 时间戳一律 UTC-naive（与 `QueueItem.available_at` 等列一致）。
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.queue_item import QueueItem, QueueItemStatus

log = get_logger(__name__)

# 主链路（任务执行）的 kind 与幂等键前缀：路由 / 调度 / 启动自愈共用，
# 避免三处各写一遍字符串后漂移。
TASK_EXEC_KIND = "task.execute"
ACTIVE_STATUSES = (QueueItemStatus.QUEUED, QueueItemStatus.RUNNING)

# 进程内 worker 标识：重启后必然不同，用来判断"续租的人还是不是同一轮执行"。
WORKER_ID = f"pid{os.getpid()}-{uuid.uuid4().hex[:8]}"[:64]


def task_dedupe_key(task_id: str) -> str:
    return f"task:{task_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive(value: datetime | None) -> datetime | None:
    """PG 下 created_at/available_at 可能回读成 tz-aware，统一归一。"""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


async def enqueue(db: AsyncSession, *, kind: str, payload: dict,
                  company_id: str | None = None, dedupe_key: str | None = None,
                  max_attempts: int = 3, delay_s: float = 0.0) -> QueueItem:
    """入队一条。dedupe_key 命中活跃条目时返回已有行，不插入新的。"""
    item, _ = await enqueue_detailed(
        db, kind=kind, payload=payload, company_id=company_id,
        dedupe_key=dedupe_key, max_attempts=max_attempts, delay_s=delay_s)
    return item


async def enqueue_detailed(db: AsyncSession, *, kind: str, payload: dict,
                           company_id: str | None = None,
                           dedupe_key: str | None = None,
                           max_attempts: int = 3,
                           delay_s: float = 0.0) -> tuple[QueueItem, bool]:
    """同 enqueue，额外返回"这次是否真的插入了新行"。

    调用方据此区分"首次投递"与"已有同一条目在排队/执行"，避免重复启动。
    """
    if dedupe_key:
        q = select(QueueItem).where(
            QueueItem.kind == kind,
            QueueItem.dedupe_key == dedupe_key,
            QueueItem.status.in_(ACTIVE_STATUSES))
        q = q.where(QueueItem.company_id.is_(None)) if company_id is None \
            else q.where(QueueItem.company_id == company_id)
        existing = (await db.execute(q.limit(1))).scalars().first()
        if existing is not None:
            return existing, False

    item = QueueItem(
        id=str(uuid.uuid4()), company_id=company_id, kind=kind,
        payload=payload or {}, dedupe_key=dedupe_key,
        status=QueueItemStatus.QUEUED,
        attempts=0, max_attempts=max(1, int(max_attempts)),
        available_at=_now() + timedelta(seconds=max(0.0, float(delay_s or 0.0))),
    )
    db.add(item)
    await db.flush()
    return item, True


def _claim_values(worker_id: str, now: datetime, lease_s: int) -> dict:
    return {
        "status": QueueItemStatus.RUNNING,
        "claimed_by": (worker_id or "worker")[:64],
        "attempts": QueueItem.attempts + 1,
        "lease_expires_at": now + timedelta(seconds=max(1, int(lease_s))),
    }


async def claim_next(db: AsyncSession, worker_id: str,
                     kinds: set[str] | None = None,
                     lease_s: int = 120) -> QueueItem | None:
    """领取一条可执行条目；没有可领取的返回 None。

    SQLite 没有 SKIP LOCKED：先选出候选，再用条件 UPDATE 抢占。
    抢输（rowcount==0）就重新选一条，最多试几次，避免长时间自旋。
    """
    worker = (worker_id or WORKER_ID)[:64]
    for _ in range(5):
        now = _now()
        q = select(QueueItem.id).where(
            QueueItem.status == QueueItemStatus.QUEUED,
            QueueItem.available_at <= now)
        if kinds:
            q = q.where(QueueItem.kind.in_(sorted(kinds)))
        candidate = (await db.execute(
            q.order_by(QueueItem.available_at, QueueItem.created_at)
            .limit(1))).scalar_one_or_none()
        if candidate is None:
            await db.commit()  # 结束只读快照，别把读事务留给下一拍
            return None

        res = await db.execute(
            update(QueueItem)
            .where(QueueItem.id == candidate,
                   QueueItem.status == QueueItemStatus.QUEUED)
            .values(**_claim_values(worker, now, lease_s))
            .execution_options(synchronize_session=False))
        await db.commit()
        if res.rowcount == 1:
            return await db.get(QueueItem, candidate, populate_existing=True)
    return None


async def claim_item(db: AsyncSession, item_id: str, worker_id: str,
                     lease_s: int = 120) -> QueueItem | None:
    """领取指定条目（启动自愈 / 重新投递用）。抢不到返回 None。

    允许抢占**租约已过期**的 RUNNING 行：那说明上一轮执行所在的进程
    已经不在了；租约仍有效的行一律不抢，避免把还在跑的任务做第二遍。
    """
    now = _now()
    res = await db.execute(
        update(QueueItem)
        .where(QueueItem.id == item_id,
               or_(QueueItem.status == QueueItemStatus.QUEUED,
                   (QueueItem.status == QueueItemStatus.RUNNING)
                   & (QueueItem.lease_expires_at.is_not(None))
                   & (QueueItem.lease_expires_at < now)))
        .values(**_claim_values(worker_id or WORKER_ID, now, lease_s))
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        return None
    return await db.get(QueueItem, item_id, populate_existing=True)


async def heartbeat(db: AsyncSession, item_id: str, worker_id: str,
                    lease_s: int = 120) -> bool:
    """续租。只有仍在执行且持有者未变的条目能被续租。"""
    res = await db.execute(
        update(QueueItem)
        .where(QueueItem.id == item_id,
               QueueItem.status == QueueItemStatus.RUNNING,
               QueueItem.claimed_by == (worker_id or WORKER_ID)[:64])
        .values(lease_expires_at=_now() + timedelta(seconds=max(1, int(lease_s))))
        .execution_options(synchronize_session=False))
    await db.commit()
    return res.rowcount == 1


async def complete(db: AsyncSession, item_id: str) -> QueueItem | None:
    res = await db.execute(
        update(QueueItem)
        .where(QueueItem.id == item_id,
               QueueItem.status.in_(ACTIVE_STATUSES))
        .values(status=QueueItemStatus.SUCCEEDED, claimed_by=None,
                lease_expires_at=None)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        return None
    return await db.get(QueueItem, item_id, populate_existing=True)


async def fail(db: AsyncSession, item_id: str, error,
               retry_backoff_s: float = 30) -> QueueItem | None:
    """标记失败：还有重试额度就退回 QUEUED（延迟重试），否则进 DEAD。

    用条件 UPDATE 而不是"读-改-写"：失败的 worker 可能已经失去租约、
    条目早被重新领取——不能把别人的新一轮执行标成失败。
    """
    err = (str(error) if error else "")[:2000] or None
    active = QueueItem.status.in_(ACTIVE_STATUSES)
    requeued = await db.execute(
        update(QueueItem)
        .where(QueueItem.id == item_id, active,
               QueueItem.attempts < QueueItem.max_attempts)
        .values(status=QueueItemStatus.QUEUED, last_error=err,
                claimed_by=None, lease_expires_at=None,
                available_at=_now() + timedelta(
                    seconds=max(0.0, float(retry_backoff_s or 0.0))))
        .execution_options(synchronize_session=False))
    if requeued.rowcount == 0:
        await db.execute(
            update(QueueItem)
            .where(QueueItem.id == item_id, active)
            .values(status=QueueItemStatus.DEAD, last_error=err,
                    claimed_by=None, lease_expires_at=None)
            .execution_options(synchronize_session=False))
    await db.commit()
    return await db.get(QueueItem, item_id, populate_existing=True)


async def cancel(db: AsyncSession, item_id: str) -> bool:
    """取消：只有还没开始的 QUEUED 条目能取消；RUNNING 返回 False。"""
    res = await db.execute(
        update(QueueItem)
        .where(QueueItem.id == item_id,
               QueueItem.status == QueueItemStatus.QUEUED)
        .values(status=QueueItemStatus.CANCELLED, claimed_by=None,
                lease_expires_at=None)
        .execution_options(synchronize_session=False))
    await db.commit()
    return res.rowcount == 1


async def cancel_active(db: AsyncSession, *, kind: str, dedupe_key: str,
                        company_id: str | None = None) -> int:
    """按幂等键取消"还没开始"的条目（任务取消时顺手清掉排队意图）。"""
    q = update(QueueItem).where(
        QueueItem.kind == kind, QueueItem.dedupe_key == dedupe_key,
        QueueItem.status == QueueItemStatus.QUEUED)
    q = q.where(QueueItem.company_id.is_(None)) if company_id is None \
        else q.where(QueueItem.company_id == company_id)
    res = await db.execute(
        q.values(status=QueueItemStatus.CANCELLED, claimed_by=None,
                 lease_expires_at=None)
        .execution_options(synchronize_session=False))
    await db.commit()
    return int(res.rowcount or 0)


async def recover_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """租约过期的 RUNNING → QUEUED（立即可再执行）。返回回收条数。"""
    now = _as_naive(now) or _now()
    res = await db.execute(
        update(QueueItem)
        .where(QueueItem.status == QueueItemStatus.RUNNING,
               QueueItem.lease_expires_at.is_not(None),
               QueueItem.lease_expires_at < now)
        .values(status=QueueItemStatus.QUEUED, claimed_by=None,
                lease_expires_at=None, available_at=now)
        .execution_options(synchronize_session=False))
    await db.commit()
    return int(res.rowcount or 0)


async def stats(db: AsyncSession, company_id: str | None = None) -> dict:
    """按状态计数 + 最老排队条目的等待秒数。"""
    count_q = select(QueueItem.status, func.count(QueueItem.id)).group_by(
        QueueItem.status)
    oldest_q = select(func.min(QueueItem.created_at)).where(
        QueueItem.status == QueueItemStatus.QUEUED)
    if company_id is not None:
        count_q = count_q.where(QueueItem.company_id == company_id)
        oldest_q = oldest_q.where(QueueItem.company_id == company_id)

    out = {s.value.lower(): 0 for s in QueueItemStatus}
    for status, n in (await db.execute(count_q)).all():
        key = status.value.lower() if hasattr(status, "value") else str(status).lower()
        out[key] = int(n or 0)
    out["total"] = sum(out[s.value.lower()] for s in QueueItemStatus)

    oldest = _as_naive((await db.execute(oldest_q)).scalar_one_or_none())
    out["oldest_queued_seconds"] = (
        round(max(0.0, (_now() - oldest).total_seconds()), 1) if oldest else 0.0)
    return out


class LeaseKeeper:
    """执行期间定期续租的守护线程。

    长任务（默认超时 900s）远超租约（120s）：不续租的话，重启自愈或
    另一个 worker 会把"仍在执行"的条目当成僵尸再跑一遍。这里用独立
    线程 + 独立 engine 续租，不干扰工作线程自己的事务。
    """

    def __init__(self, item_id: str, worker_id: str, lease_s: int = 120,
                 interval_s: float = 30.0):
        self.item_id = item_id
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.interval_s = max(1.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="aieos-queue-lease", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                asyncio.run(self._beat())
            except Exception as e:  # noqa: BLE001 — 续租失败只记日志，不打断任务
                log.warning("队列租约续租失败 item=%s: %s", self.item_id, e)

    async def _beat(self) -> None:
        from app.background_jobs import dispose_factory_engine, _make_session_factory

        factory = _make_session_factory()
        try:
            async with factory() as session:
                await heartbeat(session, self.item_id, self.worker_id,
                                self.lease_s)
        finally:
            await dispose_factory_engine(factory)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


async def recover_startup(session_factory=None, *, limit: int = 50) -> dict:
    """启动自愈：回收过期租约 + 重新投递未执行的 task.execute。

    幂等性来自两处，而不是"启动只跑一次"这个假设：
      * 只投递仍处于 QUEUED 的条目；
      * 真正执行前还要 CAS 领取（`claim_item`）——同一行被投递两次时，
        第二个 worker 领不到就直接退出，不会重复执行。

    任务本身已不在可执行状态（TODO / 已完成 / 已失败 / 已取消）时，
    排队条目直接取消：它再也不可能被执行，留着只会堵住同键去重。
    """
    from app.api.routes.tasks import _run_in_background
    from app.db.session import AsyncSessionLocal
    from app.models.task import Task, TaskStatus

    import app.background_jobs as background_jobs

    factory = session_factory or AsyncSessionLocal
    result = {"requeued": 0, "resubmitted": 0, "cancelled": 0,
              "queue_full": False}
    async with factory() as db:
        result["requeued"] = await recover_expired(db)
        pending = (await db.execute(
            select(QueueItem)
            .where(QueueItem.kind == TASK_EXEC_KIND,
                   QueueItem.status == QueueItemStatus.QUEUED)
            .order_by(QueueItem.available_at, QueueItem.created_at)
            .limit(limit))).scalars().all()
        task_ids = [(item.payload or {}).get("task_id") for item in pending]
        tasks = {}
        if task_ids:
            rows = (await db.execute(
                select(Task).where(Task.id.in_([t for t in task_ids if t]))))
            tasks = {t.id: t for t in rows.scalars()}
        await db.commit()  # 结束读事务：下面的投递会在别的线程写库

        runnable = (TaskStatus.RUNNING, TaskStatus.PLANNING)
        for item in pending:
            payload = item.payload or {}
            task_id = payload.get("task_id")
            task = tasks.get(task_id)
            agent_id = (task.assigned_agent_id if task else None) \
                or payload.get("agent_id")
            if task is None or task.status not in runnable or not agent_id:
                await cancel(db, item.id)
                result["cancelled"] += 1
                continue
            submitted = _run_in_background(task_id, agent_id,
                                           queue_item_id=item.id)
            if submitted is background_jobs.QUEUE_FULL:
                # worker 全忙：剩下的留在 QUEUED，下一次启动继续投递
                result["queue_full"] = True
                break
            result["resubmitted"] += 1
    return result
