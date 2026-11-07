"""Task state machine — illegal transitions are rejected; every change is logged.

并发安全（P0-6）：状态迁移是**抢占式 CAS**——`UPDATE tasks SET status=新
WHERE id=? AND status=旧`，rowcount != 1 说明别的请求/线程已经改过状态，
抛 IllegalTransition（路由统一转 409）。

为什么必须这样：原实现是"先读 status → 内存里赋新值 → 最后 commit"，
读与写之间没有任何互斥。双击「开始」、前端重试、双端同时操作都会让两个
请求都读到 TODO、都认为自己合法，各自提交一次后台执行——同一个任务被
执行两次、计费两次。CAS 之后只有一个赢家，另一个直接 409。
"""
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import (Column, DateTime, String, Table, delete, insert, select,
                        update)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.base import Base
from app.models.task import Task, TaskEvent, TaskEventType, TaskStatus
from app.services.event_system import log_event

# Allowed transitions (Task Status Machine)
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.WAITING, TaskStatus.PAUSED, TaskStatus.REVIEW,
                         TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.REVIEW: {TaskStatus.COMPLETED, TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.PLANNING, TaskStatus.CANCELLED},  # retry allowed
    TaskStatus.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


class ConcurrentTransition(IllegalTransition):
    """CAS 抢输：状态已经被别的请求改掉了（不是"非法转移"，而是"来晚了"）。

    单独一个类型只是为了让调用方能区分"这条迁移本来就不合法"和
    "我读到的状态已经过期"，需要区别对待时（例如取消 vs 完成）可以分开处理。
    """


async def transition(
    db: AsyncSession,
    task: Task,
    new_status: TaskStatus,
    actor_type: str = "system",
    actor_id: str | None = None,
    payload: dict | None = None,
    commit: bool = True,
) -> Task:
    old = task.status
    if new_status not in TRANSITIONS.get(old, set()):
        raise IllegalTransition(f"Illegal transition {old.value} -> {new_status.value}")

    # 抢占式 CAS：只有"读到的旧状态"仍然是库里当前状态时才会命中一行。
    # synchronize_session=False：内存对象由下面 set_committed_value 精确同步，
    # 不让 SQLAlchemy 自己猜（它可能把 object 标脏，反而在后续 flush 覆盖别人）。
    # 完成即 100%：进度条此前只读不写，已完成的任务也停在 0%。
    values: dict = {"status": new_status}
    if new_status == TaskStatus.COMPLETED:
        values["progress"] = 1.0
    res = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == old)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise ConcurrentTransition(
            f"Concurrent state change: {old.value} -> {new_status.value} "
            f"已被其他请求处理")
    # 把新状态写成"已提交值"：调用方后续读 task.status 拿到的是新状态，
    # 但不会把它当未保存修改再写一遍。
    set_committed_value(task, "status", new_status)
    if new_status == TaskStatus.COMPLETED:
        set_committed_value(task, "progress", 1.0)

    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.STATUS_CHANGED,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={"from": old.value, "to": new_status.value, **(payload or {})},
        )
    )
    await log_event(
        db, "task.status_changed", "task", task.id, actor_type, actor_id,
        {"from": old.value, "to": new_status.value},
    )
    if new_status == TaskStatus.COMPLETED:
        # 任务产出要进"待验收"，否则老板在收件箱里永远只看到"已完成"，
        # 交付/验收链路（IN_REVIEW → APPROVED → accepted_at）无从开始。
        from app.services.deliverables import record_task_deliverable
        await record_task_deliverable(db, task)
    # 完成/失败必须让任务创建人知道。通知与状态同事务：commit 由本函数
    # （commit=True）或调用方决定，通知失败只记 warning，不影响状态机。
    from app.services.notification_service import notify_task_terminal
    await notify_task_terminal(db, task, new_status.value)
    if commit:
        await db.commit()
    return task


async def add_task_event(
    db: AsyncSession, task_id: str, event_type: TaskEventType,
    actor_type: str, actor_id: str | None = None, payload: dict | None = None,
):
    db.add(TaskEvent(task_id=task_id, event_type=event_type,
                     actor_type=actor_type, actor_id=actor_id, payload=payload or {}))
    await db.flush()


# ---------------------------------------------------------------------------
# 运行时租约（会议轮次 / 工作流推进）
#
# 为什么是一张小表而不是给 meetings / workflow_executions 加 lease_until 列：
# - 加列要同时改模型与迁移，而模型目录当前有多人在并行改动，冲突面大；
# - 也不可靠地拿 updated_at 当版本：它由 server_default/func.now() 写入，
#   SQLite 下存的是不带微秒的 "YYYY-MM-DD HH:MM:SS"，而参数绑定会格式化成
#   带微秒的 "...SS.000000"，`WHERE updated_at = 读到的值` 永远不命中（实测）。
# 这张表只存"谁在什么时候之前独占某个逻辑资源"，到期自动可被抢占，
# 进程崩溃不会留下永久死锁。
# ---------------------------------------------------------------------------
runtime_locks = Table(
    "runtime_locks", Base.metadata,
    Column("lock_key", String(200), primary_key=True),
    Column("owner", String(64), nullable=False),
    Column("expires_at", DateTime, nullable=False),   # naive UTC，格式自洽可比较
)


def new_lock_owner() -> str:
    return uuid.uuid4().hex


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def acquire_lock(db: AsyncSession, key: str, owner: str,
                       ttl_seconds: float = 600.0) -> bool:
    """抢占一个带过期时间的租约；成功返回 True。

    - 无锁行 → INSERT 成功即拿到；
    - 已有锁行且租约未过期 → False；
    - 已有锁行但已过期（上一个持有者崩溃/超时）→ 条件 UPDATE 抢占，
      rowcount 决定唯一赢家。
    INSERT 冲突用 SAVEPOINT 隔离，回滚不会连带失效调用方已加载的对象。
    """
    now = _utcnow_naive()
    expires = now + timedelta(seconds=ttl_seconds)
    try:
        async with db.begin_nested():
            await db.execute(insert(runtime_locks).values(
                lock_key=key, owner=owner, expires_at=expires))
        await db.commit()
        return True
    except IntegrityError:
        # 锁行已存在 → 走下面的过期抢占；这是预期的控制流，不是吞异常
        pass  # noqa: silent-except

    res = await db.execute(
        update(runtime_locks)
        .where(runtime_locks.c.lock_key == key,
               runtime_locks.c.expires_at < now)
        .values(owner=owner, expires_at=expires)
        .execution_options(synchronize_session=False))
    await db.commit()
    return res.rowcount == 1


async def release_lock(db: AsyncSession, key: str, owner: str) -> None:
    """释放租约（只释放自己的，避免把别人的锁删掉）。"""
    await db.execute(delete(runtime_locks).where(
        runtime_locks.c.lock_key == key,
        runtime_locks.c.owner == owner))
    await db.commit()


async def lock_is_held(db: AsyncSession, key: str) -> bool:
    exp = (await db.execute(select(runtime_locks.c.expires_at).where(
        runtime_locks.c.lock_key == key))).scalar_one_or_none()
    return exp is not None and exp > _utcnow_naive()
