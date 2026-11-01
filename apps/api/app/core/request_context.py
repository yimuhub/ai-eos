"""请求上下文：让同一个 request_id 贯穿「HTTP 响应 → 日志 → 500 兜底」。

## 为什么需要它

改造前 500 兜底响应只有 `detail` 与 `error_type`，日志里也没有任何请求标识。
用户截图报错时，运维无法在成千上万行日志里定位到**那一次**请求，
跨日志关联只能靠时间戳猜。

这里用 contextvar 承载 request_id，作为唯一来源：

* `app/main.py` 的中间件写入（透传合法的 X-Request-ID，否则生成 uuid4 hex）；
* `app/core/logging_config.py` 的 Filter 读出并注入 LogRecord；
* 500 兜底处理器读出并写进响应体/响应头。

## 约束

* 只用标准库（contextvars / re / uuid），不新增依赖；
* 没有请求上下文（启动期、后台线程、定时任务、异步子任务）时统一返回
  `"-"`，保证日志 formatter 里的 `%(request_id)s` 永远不会 KeyError。
"""
from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token

REQUEST_ID_HEADER = "X-Request-ID"

# 缺省值：不在请求上下文里时使用。用 "-" 而不是空串，
# 日志里一眼能看出「这条不是请求内产生的」。
DEFAULT_REQUEST_ID = "-"

# 合法字符与长度：8–128 位 [A-Za-z0-9._-]。
# 太短容易撞车（无法区分请求），太长则是攻击面（日志膨胀/注入换行）。
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{8,128}")

_request_id_var: ContextVar[str] = ContextVar(
    "aieos_request_id", default=DEFAULT_REQUEST_ID)


def get_request_id() -> str:
    """当前上下文的 request_id；不在请求内时返回默认值 "-"。"""
    return _request_id_var.get()


def set_request_id(value: str) -> Token:
    """写入 request_id，返回可用于 reset 的 token（保持上下文干净）。"""
    return _request_id_var.set(value or DEFAULT_REQUEST_ID)


def reset_request_id(token: Token) -> None:
    """还原上下文。token 不匹配（跨任务复用）时退回默认值，绝不抛出。"""
    try:
        _request_id_var.reset(token)
    except ValueError as e:
        # contextvar 的 token 只能在其产生时的 context 里 reset；
        # 跨线程/跨任务误用时只降级，不能让日志或响应因清理动作而失败。
        logging.getLogger(__name__).debug(
            "request_id 上下文还原失败，退回默认值：%s", e)
        _request_id_var.set(DEFAULT_REQUEST_ID)


def new_request_id() -> str:
    """生成新的 request_id（uuid4 hex，32 位，满足上面的合法字符集）。"""
    return uuid.uuid4().hex


def is_valid_request_id(value: str | None) -> bool:
    """客户端透传的 X-Request-ID 是否可原样沿用。"""
    return bool(value) and _REQUEST_ID_PATTERN.fullmatch(value) is not None
