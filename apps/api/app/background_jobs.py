"""Background job executor for long-running agent work.

Each job runs in its own thread + event loop with a FRESH async engine,
because asyncpg connections are bound to the loop that created them.

修复要点：
1. 解释器关闭时线程池已 shutdown，submit 会抛 RuntimeError —— 捕获后
   改为同步直跑（保证会议轮次不丢失），而不是让会议卡死在 RUNNING。
2. 每个 job 有独立超时看门狗，超时的任务会被强制终结，
   避免任务永远停留在 RUNNING。
3. 超时按任务类型传入：会议 600s；任务/编排/工作流默认 900s，
   可用 AIEOS_TASK_TIMEOUT_S 覆盖。超时可配 on_timeout 回调，
   先写事件告知用户再标 FAILED。
4. _with_fresh_engine 的 finally 里一定 dispose engine，
   避免长服务运行下引擎/连接池对象持续泄漏。
5. 队列有界：worker 数由 AIEOS_JOB_WORKERS 控制（默认 8）；
   同时在途任务数受信号量限制，满了 submit_async 返回 QUEUE_FULL，
   调用方可据此回 503，而不是让请求在无界队列里无限堆积。
6. 排队时间计入超时预算：提交时记 enqueued_at，线程真正开始执行时
   把排队耗时从预算里扣掉，避免"排了 2 小时才开始、再跑 2 小时"。
7. 后台异常必须出声：线程体与 Future done-callback 都记录日志，
   调用方丢弃 Future 也不会再出现"任务静默失败"。
"""
import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.metrics import TASK_DURATION_SECONDS, TASK_QUEUE_DEPTH

log = logging.getLogger("background_jobs")

DEFAULT_JOB_TIMEOUT_S = 900.0
DEFAULT_JOB_WORKERS = 8


def _job_workers() -> int:
    """后台 worker 线程数（AIEOS_JOB_WORKERS，默认 8，至少 1）。"""
    try:
        n = int(os.environ.get("AIEOS_JOB_WORKERS", str(DEFAULT_JOB_WORKERS)))
    except (TypeError, ValueError):
        log.warning("环境变量 AIEOS_JOB_WORKERS 非法，回退默认 %d",
                    DEFAULT_JOB_WORKERS)
        n = DEFAULT_JOB_WORKERS
    return max(1, n)


_JOB_WORKERS = _job_workers()
_executor = ThreadPoolExecutor(max_workers=_JOB_WORKERS,
                               thread_name_prefix="aieos-job")
# 在途上限 = worker 数：队列因此有界（最多再排 _JOB_WORKERS-1 个），
# 满时直接拒绝而不是无限占用内存。
_job_slots = threading.BoundedSemaphore(_JOB_WORKERS)
_shutdown = threading.Event()

# 会议单轮看门狗：会议是短平快的轮次交互，600s 足够一轮发言+纪要提取
MEETING_TIMEOUT_S = 600


class _QueueFull:
    """submit_async 的"队列已满"哨兵（调用方可据此返回 503）。"""

    __slots__ = ()

    def __bool__(self) -> bool:  # 便于 `if not submit_async(...)` 判空
        return False

    def __repr__(self) -> str:
        return "QUEUE_FULL"


QUEUE_FULL = _QueueFull()


def capacity_snapshot() -> dict:
    """后台执行容量快照：worker 总数 + 当前空闲槽位。

    CEO 闸门用它算「系统余量」：一场统筹已经占了一个 worker，
    再吃满其余槽位会让普通任务全部排队。``_value`` 是 BoundedSemaphore
    的私有属性，但它是 CPython 的稳定实现；取不到时按 0 处理（→ 闸门走保底）。
    """
    try:
        free = int(getattr(_job_slots, "_value", 0))
    except Exception:  # noqa: BLE001
        log.debug("读取 worker 池空闲槽位失败，按 0 处理", exc_info=True)
        free = 0
    return {"workers": _JOB_WORKERS, "free": max(0, free)}


def _resolve_job_kind(coro_fn, kind: str | None = None) -> str:
    """任务 kind 只允许有限集合，避免把函数名/路径当标签造成基数爆炸。"""
    if kind:
        return str(kind)[:40]
    probe = (f"{getattr(coro_fn, '__module__', '')}."
             f"{getattr(coro_fn, '__qualname__', '')}").lower()
    for token, name in (("meeting", "meeting"),
                        ("workflow", "workflow"),
                        ("orchestrat", "orchestration"),
                        ("task", "task"),
                        ("chat", "chat")):
        if token in probe:
            return name
    return "job"


def _observe_job(kind: str, outcome: str, duration_s: float) -> None:
    """记录任务耗时；指标失败只记日志，绝不改变任务执行结果。"""
    try:
        TASK_DURATION_SECONDS.observe(
            max(0.0, float(duration_s)), kind=kind, outcome=outcome)
    except Exception as metric_err:  # noqa: BLE001 - 指标不是业务
        log.warning("任务指标记录失败 kind=%s: %s", kind, metric_err)


def _queue_depth(kind: str, delta: int) -> None:
    try:
        TASK_QUEUE_DEPTH.inc(delta, kind=kind)
    except Exception as metric_err:  # noqa: BLE001
        log.warning("队列深度指标记录失败 kind=%s: %s", kind, metric_err)


def _default_job_timeout_s() -> float:
    """任务/编排/工作流类长任务的默认超时（秒），环境变量可配。"""
    try:
        return float(os.environ.get("AIEOS_TASK_TIMEOUT_S",
                                    str(DEFAULT_JOB_TIMEOUT_S)))
    except (TypeError, ValueError):
        log.warning("环境变量 AIEOS_TASK_TIMEOUT_S 非法，回退默认 %.0fs",
                    DEFAULT_JOB_TIMEOUT_S)
        return DEFAULT_JOB_TIMEOUT_S


def _make_session_factory():
    from app.core.config import settings
    from app.db.session import SQLITE_CONNECT_TIMEOUT_S, register_sqlite_pragmas
    kwargs: dict = {"poolclass": None, "pool_pre_ping": True,
                    "echo": settings.sql_echo}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": SQLITE_CONNECT_TIMEOUT_S}
    engine = create_async_engine(settings.database_url, **kwargs)
    # 关键修复：后台任务用独立 engine，必须重新挂 PRAGMA 监听器，
    # 否则 foreign_keys 为 OFF、busy_timeout 退回 5s。
    register_sqlite_pragmas(engine)
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


async def _with_fresh_engine(coro_fn, timeout_s: float | None = None,
                             on_timeout=None, queue_delay_s: float = 0.0):
    """独立引擎 + 超时保护地执行一个异步任务。

    timeout_s 为 None 时取 AIEOS_TASK_TIMEOUT_S（默认 900s）；
    会议类调用方应显式传入 MEETING_TIMEOUT_S。
    queue_delay_s 是任务在线程池里排队等待的秒数，会从超时预算中扣除
    （排队也算任务耗时，否则"排队 2 小时再跑 2 小时"会突破总预算）。
    超时时先调 on_timeout(session, timeout)（先写事件告知用户），再向外抛 TimeoutError。
    无论成功失败，finally 里一定 dispose engine，杜绝连接池泄漏。
    """
    base_timeout = (float(timeout_s) if timeout_s is not None
                    else _default_job_timeout_s())
    queue_delay_s = max(0.0, float(queue_delay_s or 0.0))
    timeout = max(0.0, base_timeout - queue_delay_s)
    if queue_delay_s > 1.0:
        log.warning("background job 排队 %.1fs，剩余超时预算 %.1fs（总 %.1fs）",
                    queue_delay_s, timeout, base_timeout)
    if timeout <= 0:
        log.warning("background job 排队 %.1fs 已耗尽 %.1fs 超时预算，直接判定超时",
                    queue_delay_s, base_timeout)
    factory = _make_session_factory()
    try:
        async with factory() as session:
            try:
                return await asyncio.wait_for(coro_fn(session), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("background job 超时（%.0fs，含排队 %.1fs），"
                            "先写事件再标记失败", timeout, queue_delay_s)
                if on_timeout is not None:
                    try:
                        await on_timeout(session, timeout)
                    except Exception as cb_err:
                        # 超时回调本身失败不能掩盖超时事实；
                        # 残留的僵尸行由启动时的 recover_stuck_tasks 兜底
                        log.error("超时回调失败: %s", cb_err, exc_info=True)
                raise
    finally:
        # 本作业跑在 asyncio.run() 开的临时事件循环上：把网关等模块
        # 在这条循环里创建的共享 AsyncClient 关掉，避免未关闭连接随循环
        # GC 时刷 "Unclosed" 告警（客户端本身按循环作用域缓存，见 model_gateway）。
        try:
            from app.model_gateway import aclose as _gw_aclose
            await _gw_aclose()
        except Exception as gw_err:  # 关不掉也不影响作业结果
            log.debug("gateway http client 关闭失败（忽略）: %s", gw_err)
        await dispose_factory_engine(factory)


def _run_sync(coro_fn, timeout_s=None, on_timeout=None, *, kind=None):
    """在当前线程内直接跑（解释器关闭时的兜底路径）。"""
    job_kind = _resolve_job_kind(coro_fn, kind)
    started = time.perf_counter()
    outcome = "error"
    _queue_depth("running", 1)
    try:
        asyncio.run(_with_fresh_engine(coro_fn, timeout_s=timeout_s,
                                       on_timeout=on_timeout))
        outcome = "success"
    except Exception as e:
        log.error("background job (sync fallback) failed: %s: %s",
                  type(e).__name__, e)
    finally:
        _queue_depth("running", -1)
        _observe_job(job_kind, outcome, time.perf_counter() - started)


def _on_job_done(fut) -> None:
    """Future 完成回调：释放容量并保证异常一定进日志。

    调用方普遍丢弃 submit_async 的返回值，所以不能指望它们读取
    fut.exception()；统一在这里兜底记录。
    """
    try:
        if fut.cancelled():
            log.warning("background job 被取消")
            return
        exc = fut.exception()
        if exc is not None:
            log.error("background job 异常退出: %s: %s",
                      type(exc).__name__, exc)
    except Exception as cb_err:  # 回调自身失败不能影响容量释放
        log.error("background job done-callback 失败: %s", cb_err,
                  exc_info=True)
    finally:
        _job_slots.release()


def submit_async(coro_fn, timeout_s: float | None = None, on_timeout=None,
                 *, kind: str | None = None):
    """Run an async function in a worker thread with its own loop + engine.

    timeout_s: 超时秒数；None 则取 AIEOS_TASK_TIMEOUT_S（默认 900s）；
    会议类调用方请显式传 MEETING_TIMEOUT_S(600)。
    on_timeout: 超时回调 async (session, timeout_s)，用于先写事件告知用户再标 FAILED。
    kind: 指标里的任务类型（task/meeting/workflow/...）；缺省按调用方模块推断。

    返回：
      * Future —— 已入队（调用方可以不等，但队列已满时不会静默堆积）；
      * QUEUE_FULL —— 在途任务已达上限，调用方应返回 503；
      * None —— 线程池已关闭（解释器退出中），已退化为同步执行，任务不丢。
    """
    job_kind = _resolve_job_kind(coro_fn, kind)
    if _shutdown.is_set():
        # 解释器正在关闭：直接在当前线程跑，保证最后一轮不丢
        _run_sync(coro_fn, timeout_s=timeout_s, on_timeout=on_timeout,
                  kind=job_kind)
        return None
    if not _job_slots.acquire(blocking=False):
        log.warning("后台任务队列已满（%d 个 worker 全部忙碌），本次提交被拒绝",
                    _JOB_WORKERS)
        return QUEUE_FULL

    _queue_depth("pending", 1)
    enqueued_at = time.monotonic()

    def _runner():
        queue_delay = max(0.0, time.monotonic() - enqueued_at)
        _queue_depth("pending", -1)
        _queue_depth("running", 1)
        started = time.perf_counter()
        outcome = "error"
        try:
            asyncio.run(_with_fresh_engine(coro_fn, timeout_s=timeout_s,
                                           on_timeout=on_timeout,
                                           queue_delay_s=queue_delay))
            outcome = "success"
        except BaseException:
            # 含 CancelledError/SystemExit：线程里的异常不会再冒泡到调用方，
            # 不在这里记录就等于永久丢失。
            log.exception("background job 执行失败（排队 %.1fs）", queue_delay)
            raise
        finally:
            _queue_depth("running", -1)
            _observe_job(job_kind, outcome, time.perf_counter() - started)

    try:
        fut = _executor.submit(_runner)
    except RuntimeError:
        # cannot schedule new futures after interpreter shutdown
        _queue_depth("pending", -1)
        _job_slots.release()
        _shutdown.set()
        _run_sync(coro_fn, timeout_s=timeout_s, on_timeout=on_timeout,
                  kind=job_kind)
        return None
    except BaseException:
        _queue_depth("pending", -1)
        _job_slots.release()
        raise
    fut.add_done_callback(_on_job_done)
    return fut


def submit(fn):
    """Submit a blocking callable to the shared executor."""
    if _shutdown.is_set():
        return None
    if not _job_slots.acquire(blocking=False):
        log.warning("后台任务队列已满（%d 个 worker 全部忙碌），本次提交被拒绝",
                    _JOB_WORKERS)
        return None
    try:
        fut = _executor.submit(fn)
    except RuntimeError:
        _job_slots.release()
        _shutdown.set()
        return None
    except BaseException:
        _job_slots.release()
        raise
    fut.add_done_callback(_on_job_done)
    return fut


def run_meeting_turn_now(meeting_id: str):
    """立即（阻塞式）推进一轮会议 —— 供自愈/手动恢复调用。"""
    from app.meeting_engine import run_meeting_turn

    async def _job(session):
        result = await run_meeting_turn(session, meeting_id)
        log.info("[meeting %s] turn done: %s", meeting_id, result)

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S, kind="meeting")


def recover_stuck_meetings(max_age_minutes: int = 15):
    """把卡在 RUNNING 且长时间无新发言的会议重新踢一脚。

    在应用启动时调用一次即可。
    """
    async def _job(session):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from app.models.meeting import Meeting, MeetingMessage, MeetingStatus

        # 与 recover_stuck_tasks 同法： MeetingMessage.created_at 在 PG 下是
        # tz-aware（DateTime(timezone=True) + server_default now()），直接拿
        # datetime.utcnow() 比较会抛 TypeError；统一按 naive UTC 归一。
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(minutes=max_age_minutes))
        q = await session.execute(
            select(Meeting).where(Meeting.status == MeetingStatus.RUNNING))
        stuck = q.scalars().all()
        for m in stuck:
            last = await session.execute(
                select(MeetingMessage.created_at).where(
                    MeetingMessage.meeting_id == m.id).
                order_by(MeetingMessage.created_at.desc()).limit(1))
            last_at = last.scalar_one_or_none()
            if last_at is not None:
                if last_at.tzinfo is not None:
                    last_at = last_at.replace(tzinfo=None)
                if last_at > cutoff:
                    continue  # 还在正常推进，别打扰
            log.warning("[recover] re-kick stuck meeting %s", m.id)
            schedule_kick(str(m.id))

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S, kind="meeting")


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

    submit_async(_job, timeout_s=MEETING_TIMEOUT_S, kind="meeting")


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
                # transition 已覆盖正常的 FAILED 路径；这里是"非法转移兜底"
                # 分支，同样要让创建人知道（否则任务静默变成失败）。
                from app.services.notification_service import notify_task_terminal
                await notify_task_terminal(session, t, "FAILED",
                                           error="任务长时间无心跳，已被系统回收")
                await session.commit()
            await log_event(session, "task.stuck_recovered", "task", t.id,
                            "system", None,
                            {"from_status": from_status,
                             "max_age_minutes": max_age_minutes})
            await session.commit()

    submit_async(_job, kind="task")
