"""SQLite 并发与后台执行器护栏。

覆盖三类曾经"静默失效"的问题：

1. 连接级 PRAGMA：``busy_timeout`` 只有 5s（并发写立刻 locked）、
   后台任务另建 engine 时漏挂监听器导致 ``foreign_keys`` 实际为 OFF。
2. ``submit_async`` 的 Future 被调用方丢弃后，后台异常既不进日志
   也不影响任何状态——任务失败"无痕"。
3. 调度器"读到期的 → 执行 → 改 next_run_at"没有原子抢占，
   两个扫描者（后台循环 + tick）会把同一动作执行两次。

测试全部使用 conftest 的临时 SQLite 文件，不碰 ``apps/api/data/``
下的真实库。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text

from app.db.session import AsyncSessionLocal, engine
from app.models.scheduler import AutomationRun, AutomationSchedule
from app import background_jobs, scheduler_engine


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 1) PRAGMA：主 engine 与后台任务 engine 必须一致
# ---------------------------------------------------------------------------


async def test_main_engine_enables_busy_timeout_and_foreign_keys():
    async with engine.connect() as conn:
        busy = int((await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar())
        fk = int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar())
        journal = str((await conn.exec_driver_sql(
            "PRAGMA journal_mode")).scalar()).lower()

    assert busy >= 10_000, f"busy_timeout 过低（{busy}ms），并发写会立刻 locked"
    assert fk == 1, "主 engine 连接的外键约束未开启"
    assert journal == "wal"


async def test_background_factory_applies_same_pragmas():
    """后台任务用独立 engine，必须重新挂 PRAGMA 监听器。

    曾经的缺陷：_make_session_factory 新建 engine 时没挂监听器，
    后台连接 foreign_keys=0 —— 外键只写不校验，脏数据静默落库。
    """
    factory = background_jobs._make_session_factory()
    try:
        async with factory() as session:
            busy = int((await session.execute(
                text("PRAGMA busy_timeout"))).scalar())
            fk = int((await session.execute(
                text("PRAGMA foreign_keys"))).scalar())
    finally:
        await background_jobs.dispose_factory_engine(factory)

    assert busy >= 10_000, f"后台 engine busy_timeout 过低（{busy}ms）"
    assert fk == 1, "后台 engine 连接的外键约束未开启"


# ---------------------------------------------------------------------------
# 2) 后台异常必须进日志（调用方丢弃 Future 也不影响）
# ---------------------------------------------------------------------------


def test_submit_async_logs_job_failure(caplog):
    async def _boom(_session):
        raise RuntimeError("boom-from-background-job")

    with caplog.at_level(logging.ERROR, logger="background_jobs"):
        fut = background_jobs.submit_async(_boom, timeout_s=10)
        assert fut is not None, "线程池未关闭时不应退化为同步执行"
        assert fut is not background_jobs.QUEUE_FULL, "测试环境下队列不应为满"
        # 异常要同时满足两件事：读 Future 的调用方能拿到，没读的调用方
        # 也能在日志里看到（done-callback 兜底），而不是被静默吞掉。
        with pytest.raises(RuntimeError):
            fut.result(timeout=30)

    assert "boom-from-background-job" in caplog.text, (
        "后台任务异常没有进日志：调用方丢弃 Future 时故障将完全无痕")


# ---------------------------------------------------------------------------
# 3) 调度原子抢占：同一 due 记录并发扫描只执行一次
# ---------------------------------------------------------------------------


async def _seed_due_schedule(schedule_id: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(AutomationSchedule(
            id=schedule_id,
            company_id="company-concurrency-test",
            name="并发抢占用例",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            action_type="snapshot_performance",
            action_params={},
            status="ACTIVE",
            next_run_at=_naive_utc_now(),
            attachments=[],
        ))
        await session.commit()


async def test_concurrent_run_due_schedules_executes_once(monkeypatch):
    schedule_id = "sched-concurrency-0001"
    await _seed_due_schedule(schedule_id)

    calls: list[str] = []

    async def _fake_execute(db, sched):
        calls.append(sched.id)
        # 拉长执行窗口，确保两次扫描的抢占确实重叠
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(scheduler_engine, "execute_action", _fake_execute)

    async def _scan() -> int:
        async with AsyncSessionLocal() as session:
            return await scheduler_engine.run_due_schedules(session)

    counts = await asyncio.gather(_scan(), _scan())

    assert len(calls) == 1, f"同一调度被执行了 {len(calls)} 次：{calls}"
    assert sum(counts) == 1, f"抢占计数应为 1，实际 {counts}"

    async with AsyncSessionLocal() as session:
        sched = await session.get(AutomationSchedule, schedule_id)
        assert sched is not None
        assert sched.run_count == 1, "run_count 应只增加一次"
        assert sched.next_run_at is not None
        assert sched.next_run_at > _naive_utc_now(), "执行后必须写入真正的下次运行时间"
        run_total = (await session.execute(
            select(func.count()).select_from(AutomationRun)
            .where(AutomationRun.schedule_id == schedule_id))).scalar()
    assert run_total == 1, f"运行记录写了 {run_total} 条（应幂等为 1 条）"
