"""CEO 并发闸门：保底 2、默认 3、可调 3–10，按供应商能力封顶。

## 为什么不是 asyncio.gather 全量并发

旧 `orchestrator` 把所有子任务一把 `gather` 出去。供应商只允许 2~3 个并发时，
第 4 个开始就是 429；重试三次后整批失败，老板看到的是「全部子任务都挂了」，
而不是「第 4 个排在后面」。

## 三层取最小

    effective = max(MIN, min(老板设定, 供应商档位, 系统余量))

* **老板设定**：界面可调 3–10（`AIEOS_CEO_MAX_PARALLEL` 改默认）
* **供应商档位**：按负责人的主模型 provider 给一个保守上限；
  没有模型/未知供应商按 2（保底，任何供应商都扛得住）
* **系统余量**：后台 worker 还要留给普通任务，不为一场统筹吃满整个池

## 自动降档

连续 3 次「限流 / 超时」→ 立刻降到保底 2 并冷却 5 分钟；
冷却到期后由下一次调度试探性恢复。降档不丢任务：闸门只是少发许可，
任务仍在内存队列里等下一个空位。降档原因写进 mission 与审计事件，
让老板在面板上看到「今天为什么只开了 2 个」，而不是猜。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent, AgentModelConfig, Model

logger = get_logger(__name__)

MIN_PARALLEL = 2
DEFAULT_PARALLEL = 3
HARD_CAP = 10

DEGRADE_AFTER_FAILURES = 3
DEGRADE_COOLDOWN_S = 300

# 供应商档位：保守值。409/429 的实测经验是「未知供应商最坏只给 2」，
# 因此默认值必须等于保底而不是默认并发，否则「保底」就成了空话。
PROVIDER_CAPS: dict[str, int] = {
    "openai_compat": 8,
    "anthropic": 4,
    "gemini": 3,
}
UNKNOWN_PROVIDER_CAP = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clamp_requested(value: int | None) -> int:
    """老板设定：非法值回落到默认；超界夹到 [MIN, HARD_CAP]。"""
    try:
        n = int(value if value is not None else DEFAULT_PARALLEL)
    except (TypeError, ValueError):
        return DEFAULT_PARALLEL
    return max(MIN_PARALLEL, min(HARD_CAP, n))


def provider_cap_of(provider_type: str | None) -> int:
    key = (provider_type or "").strip().lower()
    return PROVIDER_CAPS.get(key, UNKNOWN_PROVIDER_CAP)


async def _coordinator_provider(db: AsyncSession, agent_id: str | None) -> str | None:
    """负责人的主模型供应商；没有绑定就返回 None（→ 未知档位 2）。"""
    if not agent_id:
        return None
    row = (await db.execute(
        select(Model.provider_type)
        .join(AgentModelConfig, AgentModelConfig.model_id == Model.id)
        .where(AgentModelConfig.agent_id == agent_id)
        .order_by(AgentModelConfig.created_at)
        .limit(1))).first()
    if not row:
        return None
    value = row[0]
    return getattr(value, "value", value)


def _worker_free() -> int:
    """后台 worker 空闲槽位；取不到时按"至少能开 2 个"处理。"""
    try:
        import app.background_jobs as bg
        return int(bg.capacity_snapshot().get("free", 0))
    except Exception:  # noqa: BLE001 — 容量探测失败不能阻断统筹
        logger.warning("后台 worker 空闲槽位探测失败，统筹按保底并发 %s 处理",
                       MIN_PARALLEL, exc_info=True)
        return MIN_PARALLEL


async def resolve_capacity(db: AsyncSession, coordinator_agent_id: str | None,
                           requested: int | None) -> dict:
    """算出本次统筹的真实并发上限，并把每一层的值都返回给前端展示。"""
    want = clamp_requested(requested)
    provider = await _coordinator_provider(db, coordinator_agent_id)
    p_cap = provider_cap_of(provider)
    free = max(MIN_PARALLEL, _worker_free())
    effective = max(MIN_PARALLEL, min(want, p_cap, free))
    return {
        "requested": want,
        "provider_cap": p_cap,
        "provider_type": provider or "unknown",
        "worker_free": free,
        "effective": effective,
        "min_parallel": MIN_PARALLEL,
        "hard_cap": HARD_CAP,
        "default_parallel": DEFAULT_PARALLEL,
    }


def failure_kind(exc: BaseException | None) -> str:
    """把一个执行异常归类：限流 / 超时 / 其他。"""
    if exc is None:
        return "other"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "throttle"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "other"


class DynamicGate:
    """可动态收放的并发闸门（asyncio 条件变量实现）。

    只在一场 mission 的后台任务内部使用，不跨进程——跨进程的并发计数
    需要租约表，那是另一套复杂度；一场统筹占一个 worker，天然有界。
    """

    def __init__(self, limit: int, base_limit: int | None = None):
        self.limit = max(MIN_PARALLEL, int(limit))
        self.base_limit = max(MIN_PARALLEL, int(base_limit or limit))
        self.active = 0
        self.paused = False
        self.degraded = False
        self.degrade_reason: str | None = None
        self._degraded_until: datetime | None = None
        self._consecutive_failures = 0
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()

    async def acquire(self) -> None:
        async with self._cond:
            while True:
                self._restore_if_due()
                if not self.paused and self.active < self.limit:
                    self.active += 1
                    return
                await self._cond.wait()

    async def release(self) -> None:
        async with self._cond:
            if self.active > 0:
                self.active -= 1
            self._cond.notify_all()

    async def set_paused(self, paused: bool) -> None:
        async with self._cond:
            self.paused = paused
            self._cond.notify_all()

    async def note_success(self) -> None:
        async with self._cond:
            self._consecutive_failures = 0

    async def note_failure(self, kind: str) -> dict | None:
        """连续限流/超时达到阈值就降档；返回降档信息（未触发返回 None）。"""
        if kind not in ("throttle", "timeout"):
            return None
        async with self._cond:
            self._consecutive_failures += 1
            if self._consecutive_failures < DEGRADE_AFTER_FAILURES:
                return None
            if self.degraded and self.limit <= MIN_PARALLEL:
                return None
            self.degraded = True
            self.limit = MIN_PARALLEL
            self.degrade_reason = (
                f"连续 {self._consecutive_failures} 次"
                f"{'上游限流' if kind == 'throttle' else '请求超时'}，"
                f"并发已降为 {MIN_PARALLEL}，冷却 "
                f"{DEGRADE_COOLDOWN_S // 60} 分钟后自动恢复")
            self._degraded_until = _now() + timedelta(seconds=DEGRADE_COOLDOWN_S)
            self._consecutive_failures = 0
            self._cond.notify_all()
            return {
                "degraded": True,
                "limit": self.limit,
                "reason": self.degrade_reason,
                "until": self._degraded_until.isoformat(),
            }

    def _restore_if_due(self) -> None:
        if not self.degraded or self._degraded_until is None:
            return
        if _now() >= self._degraded_until:
            self.degraded = False
            self.degrade_reason = None
            self._degraded_until = None
            self.limit = self.base_limit

    def snapshot(self) -> dict:
        self._restore_if_due()
        return {
            "limit": self.limit,
            "active": self.active,
            "paused": self.paused,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "degraded_until": (self._degraded_until.isoformat()
                               if self._degraded_until else None),
        }


async def current_capacity(db: AsyncSession, company_id: str,
                           coordinator_agent_id: str | None = None,
                           requested: int | None = None) -> dict:
    """给前端看的能力快照：不查 mission，直接按公司/负责人算一次。"""
    if coordinator_agent_id is None:
        row = (await db.execute(
            select(Agent.id).where(
                Agent.company_id == company_id,
                Agent.is_ceo.is_(True))
            .limit(1))).first()
        coordinator_agent_id = row[0] if row else None
    return await resolve_capacity(db, coordinator_agent_id, requested)
