"""trace_id 贯穿（V6.1 可观测性）：任务/会话/工作流 → 工具 → 模型调用。

一次业务动作（一个任务、一次会话、一个工作流节点）拥有同一个 trace_id，
event_log 的 payload 里带上它——绩效中心的 trace 查询就能把「一次任务里
模型调了什么、工具干了什么、哪一步出错」串成一条链。

用法：
    with trace_scope():            # 入口处开一条 trace（或 trace_scope(tid)）
        ...                        # 期间所有 log_event 自动携带
    payload = traced_payload({"ok": True})   # 审计 payload 附加 trace_id

contextvar 天然兼容 asyncio.gather 并行（每个 worker 继承入口快照）。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from contextlib import contextmanager

_TRACE_ID: ContextVar[str | None] = ContextVar("aieos_trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


@contextmanager
def trace_scope(trace_id: str | None = None):
    """开一条 trace 作用域；不传 id 则生成新的。"""
    token = _TRACE_ID.set(trace_id or new_trace_id())
    try:
        yield _TRACE_ID.get()
    finally:
        _TRACE_ID.reset(token)


def begin_trace(trace_id: str | None = None):
    """非 contextmanager 入口：设置当前 trace，返回 token 供 end_trace 复原。

    适合已有 try/finally 结构的长函数（run_agent_on_task / 聊天流），
    避免为包一层 with 整体缩进。当前 trace_id 用 get_trace_id() 取。
    """
    token = _TRACE_ID.set(trace_id or new_trace_id())
    return token


def end_trace(token) -> None:
    _TRACE_ID.reset(token)


def traced_payload(payload: dict | None) -> dict:
    """给事件 payload 附加 trace_id（没有活动 trace 时原样返回）。"""
    tid = _TRACE_ID.get()
    if not tid:
        return payload or {}
    merged = dict(payload or {})
    merged.setdefault("trace_id", tid)
    return merged
