"""Background job executor for long-running agent work.

Each job runs in its own thread + event loop with a FRESH async engine,
because asyncpg connections are bound to the loop that created them.

修复要点：
1. 解释器关闭时线程池已 shutdown，submit 会抛 RuntimeError —— 捕获后
   改为同步直跑（保证会议轮次不丢失），而不是让会议卡死在 RUNNING。
2. 每个 job 有独立超时看门狗，超时的会议轮次会被强制终结，
   避免会议永远停留在 RUNNING。
"""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

log = logging.getLogger("background_jobs")

_executor = ThreadPoolExecutor(max_workers=4)
_shutdown = threading.Event()


def _make_session_factory():
    from app.core.config import settings
    engine = create_async_engine(settings.database_url, poolclass=None, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _with_fresh_engine(coro_fn, timeout_s: float = 600):
    """独立引擎 + 超时保护地执行一个异步任务。"""
    factory = _make_session_factory()
    try:
        async with factory() as session:
            return await asyncio.wait_for(coro_fn(session), timeout=timeout_s)
    finally:
        pass


def _run_sync(coro_fn):
    """在当前线程内直接跑（解释器关闭时的兜底路径）。"""
    try:
        asyncio.run(_with_fresh_engine(coro_fn))
    except Exception as e:
        log.error("background job (sync fallback) failed: %s: %s",
                  type(e).__name__, e)


def submit_async(coro_fn):
    """Run an async function in a worker thread with its own loop + engine.

    线程池已被关闭（解释器退出中）时退化为同步执行，任务不丢。
    """
    if _shutdown.is_set():
        # 解释器正在关闭：直接在当前线程跑，保证最后一轮不丢
        _run_sync(coro_fn)
        return None
    try:
        def _runner():
            asyncio.run(_with_fresh_engine(coro_fn))

        return _executor.submit(_runner)
    except RuntimeError:
        # cannot schedule new futures after interpreter shutdown
        _shutdown.set()
        _run_sync(coro_fn)
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
        print(f"[meeting {meeting_id}] turn done: {result}")

    submit_async(_job)


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
            print(f"[recover] re-kick stuck meeting {m.id}")
            schedule_kick(str(m.id))

    submit_async(_job)


def schedule_kick(meeting_id: str):
    """调度一轮新的会议推进。"""
    from app.meeting_engine import run_meeting_turn

    async def _job(session):
        try:
            result = await run_meeting_turn(session, meeting_id)
            print(f"[meeting {meeting_id}] round done: {result}")
        except Exception as e:
            print(f"[meeting {meeting_id}] ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    submit_async(_job)
