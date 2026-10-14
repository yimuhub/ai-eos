"""Background job executor for long-running agent work.

Each job runs in its own thread + event loop with a FRESH async engine,
because asyncpg connections are bound to the loop that created them.

修复要点：
1. 解释器关闭时线程池已 shutdown，submit 会抛 RuntimeError —— 捕获后
   改为同步直跑（保证会议轮次不丢失），而不是让会议卡死在 RUNNING。
2. 每个 job 有独立超时看门狗，超时的任务会被强制终结，
   避免任务永远停留在 RUNNING。
3. 超时按任务类型传入：会议 600s；任务/编排/工作流走环境变量
   AIEOS_TASK_TIMEOUT_S（默认 7200s）。超时可配 on_timeout 回调，
   先写事件告知用户再标 FAILED。
4. _with_fresh_engine 的 finally 里一定 dispose engine，
   避免长服务运行下引擎/连接池对象持续泄漏。
"""
import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

log = logging.getLogger("background_jobs")

_executor = ThreadPoolExecutor(max_workers=4)
_shutdown = threading.Event()

# 会议单轮看门狗：会议是短平快的轮次交互，600s 足够一轮发言+纪要提取
MEETING_TIMEOUT_S = 600


def _default_job_timeout_s() -> float:
    """任务/编排/工作流类长任务的默认超时（秒），环境变量可配。"""
    try:
        return float(os.environ.get("AIEOS_TASK_TIMEOUT_S", "7200"))
    except (TypeError, ValueError):
        log.warning("环境变量 AIEOS_TASK_TIMEOUT_S 非法，回退默认 7200s")
        return 7200.0


def _make_session_factory():
    from app.core.config import settings
    engine = create_async_engine(settings.database_url, poolclass=None, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # 把 engine 挂在 factory 上：_with_fresh_engine 在 finally 里 dispose，
    # 避免长服务运行下引擎/连接池对象持续泄漏。挂载属性不影响 factory() 调用。
    factory._engine = engine
    return factory


async def dispose_factory_engine(factory) -> None:
    """释放 _make_session_factory() 创建的底层 engine（没有则忽略）。"""
    engine = getattr(factory, "_engine", None)
    if engine is not None:
        try:
            await engine.dispose()
        except Exception as e:
            log.warning("engine dispose 失败: %s", e)


async def _with_fresh_engine(coro_fn, timeout_s: float | None = None, on_timeout=None):
    """独立引擎 + 超时保护地执行一个异步任务。

    timeout_s 为 None 时取 AIEOS_TASK_TIMEOUT_S（默认 7200s）；
    会议类调用方应显式传入 MEETING_TIMEOUT_S。
    超时时先调 on_timeout(session, timeout)（先写事件告知用户），再向外抛 TimeoutError。
    无论成功失败，finally 里一定 dispose engine，杜绝连接池泄漏。
    """
    timeout = float(timeout_s) if timeout_s is not None else _default_job_timeout_s()
    factory = _make_session_factory()
    try:
        async with factory() as session:
            try:
                return await asyncio.wait_for(coro_fn(session), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("background job 超时（%.0fs），先写事件再标记失败", timeout)
                if on_timeout is not None:
                    try:
                        await on_timeout(session, timeout)
                    except Exception as cb_err:
                        # 超时回调本身失败不能掩盖超时事实；
                        # 残留的僵尸行由启动时的 recover_stuck_tasks 兜底
                        log.error("超时回调失败: %s", cb_err, exc_info=True)
                raise
    finally:
        await dispose_factory_engine(factory)


def _run_sync(coro_fn, timeout_s=None, on_timeout=None):
    """在当前线程内直接跑（解释器关闭时的兜底路径）。"""
    try:
        asyncio.run(_with_fresh_engine(coro_fn, timeout_s=timeout_s,
                                       on_timeout=on_timeout))
    except Exception as e:
        log.error("background job (sync fallback) failed: %s: %s",
                  type(e).__name__, e)


def submit_async(coro_fn, timeout_s: float | None = None, on_timeout=None):
    """Run an async function in a worker thread with its own loop + engine.

    timeout_s: 超时秒数；None 则取 AIEOS_TASK_TIMEOUT_S（默认 7200s）；
    会议类调用方请显式传 MEETING_TIMEOUT_S(600)。
    on_timeout: 超时回调 async (session, timeout_s)，用于先写事件告知用户再标 FAILED。

    线程池已被关闭（解释器退出中）时退化为同步执行，任务不丢。
    """
    if _shutdown.is_set():
        # 解释器正在关闭：直接在当前线程跑，保证最后一轮不丢
        _run_sync(coro_fn, timeout_s=timeout_s, on_timeout=on_timeout)
        return None
    try:
        def _runner():
            asyncio.run(_with_fresh_engine(coro_fn, timeout_s=timeout_s,
                                           on_timeout=on_timeout))

        return _executor.submit(_runner)
    except RuntimeError:
        # cannot schedule new futures after interpreter shutdown
        _shutdown.set()
        _run_sync(coro_fn, timeout_s=timeout_s, on_timeout=on_timeout)
        return None


def submit(fn):
    """Submit a blocking callable to the shared executor."""
    if _shutdown.is_set():
        return None
    try:
        return _executor.submit(fn)
    except RuntimeError:
        _shutdown.set()
        return None


def run_meeting_turn_now(meeting_id: str):
    """立即（阻塞式）推进一轮会议 —— 供自愈/手动恢复调用。"""
    from app.meeting_engine import run_meeting_turn

    async def _job(session):
        result = await run_meeting_turn(session, meeting_id)
        log.info("[meeting %s] turn done: %s", meeting_id, result)

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S)


def recover_stuck_meetings(max_age_minutes: int = 15):
    """把卡在 RUNNING 且长时间无新发言的会议重新踢一脚。

    在应用启动时调用一次即可。
    """
    async def _job(session):
        from datetime import datetime, timedelta

        from sqlalchemy import select

        from app.models.meeting import Meeting, MeetingMessage, MeetingStatus

        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        q = await session.execute(
            select(Meeting).where(Meeting.status == MeetingStatus.RUNNING))
        stuck = q.scalars().all()
        for m in stuck:
            last = await session.execute(
                select(MeetingMessage.created_at).where(
                    MeetingMessage.meeting_id == m.id).
                order_by(MeetingMessage.created_at.desc()).limit(1))
            last_at = last.scalar_one_or_none()
            if last_at is not None and last_at > cutoff:
                continue  # 还在正常推进，别打扰
            log.warning("[recover] re-kick stuck meeting %s", m.id)
            schedule_kick(str(m.id))

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S)


def schedule_kick(meeting_id: str):
    """调度一轮新的会议推进。"""
    from app.meeting_engine import run_meeting_turn

    async def _job(session):
        try:
            result = await run_meeting_turn(session, meeting_id)
            log.info("[meeting %s] round done: %s", meeting_id, result)
        except Exception as e:
            # 用结构化日志而不是 print_exc：print 走 stdout，
            # 会绕过日志级别与统一格式，线上排查时反而更难定位。
            log.error("[meeting %s] ERROR: %s: %s",
                      meeting_id, type(e).__name__, e, exc_info=True)

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S)


def _stuck_task_minutes() -> int:
    """僵尸任务判定阈值（分钟），环境变量可配。"""
    try:
        return int(os.environ.get("AIEOS_STUCK_TASK_MINUTES", "30"))
    except (TypeError, ValueError):
        return 30


def recover_stuck_tasks(max_age_minutes: int | None = None):
    """把卡在 RUNNING/PAUSED 且长时间无事件心跳的任务标 FAILED 并写事件。

    对标 recover_stuck_meetings：在应用启动时调用一次，清理服务重启后
    留在 DB 里永远"执行中"的僵尸任务行。max_age_minutes 默认 30，
    可用环境变量 AIEOS_STUCK_TASK_MINUTES 覆盖。
    """
    if max_age_minutes is None:
        max_age_minutes = _stuck_task_minutes()

    async def _job(session):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from app.models.task import Task, TaskEvent, TaskStatus
        from app.services.event_system import log_event
        from app.task_engine import IllegalTransition, transition

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = cutoff - timedelta(minutes=max_age_minutes)
        q = await session.execute(
            select(Task).where(Task.status.in_([TaskStatus.RUNNING, TaskStatus.PAUSED])))
        stuck = q.scalars().all()
        for t in stuck:
            last = await session.execute(
                select(TaskEvent.created_at).where(TaskEvent.task_id == t.id).
                order_by(TaskEvent.created_at.desc()).limit(1))
            last_at = last.scalar_one_or_none()
            if last_at is not None:
                # created_at 在 PG 下是 tz-aware，SQLite 下是 naive：统一按 naive 比较
                if last_at.tzinfo is not None:
                    last_at = last_at.replace(tzinfo=None)
                if last_at > cutoff:
                    continue  # 还有心跳，别打扰
            from_status = t.status.value
            log.warning("[recover] 任务 %s 卡在 %s 超过 %d 分钟无心跳，标 FAILED",
                        t.id, from_status, max_age_minutes)
            try:
                # PAUSED→FAILED 不是合法转移：transition 抛错时直接置状态兜底
                await transition(session, t, TaskStatus.FAILED, actor_type="system",
                                 payload={"reason": "stuck_recovered",
                                          "max_age_minutes": max_age_minutes})
            except IllegalTransition:
                t.status = TaskStatus.FAILED
                await session.commit()
            await log_event(session, "task.stuck_recovered", "task", t.id,
                            "system", None,
                            {"from_status": from_status,
                             "max_age_minutes": max_age_minutes})
            await session.commit()

    submit_async(_job)
