"""统一错误信封：所有错误响应都能被前端按同一份契约解析。

## 为什么需要它

改造前错误响应有三种形态：``{"detail": "..."}``、裸 500 的
``{"detail", "error_type", "request_id"}``、以及只读模式中间件直接返回的
``{"detail", "code"}``。前端要写三套分支才能取到一句可展示的话，
排障时也拿不到统一的 ``request_id``。

这里只做**收敛，不改变语义**：

* 状态码一律保持原值（由调用方/HTTPException 决定）；
* ``detail`` 原样保留（字符串 / 422 的 errors 列表 / 对象都不改），
  现有前端与测试继续读它；
* 在此之上补齐 ``code`` / ``message`` / ``request_id``。

``request_id`` 的唯一来源仍是 :mod:`app.core.request_context`，
本模块不另造一套生成/透传机制。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# 状态码 → 稳定错误码。外部集成方按 code 分支，比按中文文案匹配可靠。
_STATUS_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
}

# 复杂 detail（列表/对象）没有可复用的字符串时使用的通用文案。
_CODE_MESSAGES: dict[str, str] = {
    "bad_request": "请求参数有误",
    "unauthorized": "未登录或登录已失效",
    "forbidden": "没有执行该操作的权限",
    "license_read_only": "License 已过期或无效，系统处于只读模式",
    "not_found": "请求的资源不存在",
    "method_not_allowed": "请求方法不被允许",
    "conflict": "当前状态与该操作冲突",
    "validation_error": "请求参数校验失败",
    "rate_limited": "请求过于频繁，请稍后重试",
    "internal_error": "服务器内部错误，请查看后端日志获取详细堆栈",
}

_GENERIC_MESSAGE = "请求失败"


def error_code_for_status(status: int, detail: Any = None) -> str:
    """把 HTTP 状态码映射成稳定错误码。

    优先级：detail 里已带 ``code``（如只读模式的 ``license_read_only``）
    就保留它——上层显式给的语义比状态码更精确；否则按状态码映射，
    未知状态退化为 ``http_{status}``（绝不返回空串）。
    """
    if isinstance(detail, dict):
        existing = detail.get("code")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
    elif isinstance(detail, str) and "license_read_only" in detail:
        return "license_read_only"
    try:
        code = int(status)
    except (TypeError, ValueError):
        return "internal_error"
    return _STATUS_CODE_MAP.get(code, f"http_{code}")


def message_for_code(code: str) -> str:
    """错误码对应的通用文案（detail 不是字符串时使用）。"""
    return _CODE_MESSAGES.get(code, _GENERIC_MESSAGE)


def envelope(detail: Any, code: str, request_id: str, **extra: Any) -> dict:
    """构造统一错误体：``{detail, code, message, request_id, **extra}``。

    ``detail`` 原样保留；``message`` 对字符串 detail 直接复用（保证前端
    展示的仍是后端写好的那句话），对列表/对象用通用文案。``extra`` 用于
    保留各处理器原有的附加字段（如 500 的 ``error_type``）。
    """
    if isinstance(detail, str) and detail.strip():
        message = detail
    else:
        message = message_for_code(code)
    body: dict[str, Any] = {
        "detail": detail,
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    body.update(extra)
    return body


class ApiError(HTTPException):
    """可选的结构化错误异常：``raise ApiError(403, "...", code="...")``。

    继承 HTTPException，因此复用同一个处理器；额外携带的 ``code`` 与
    ``extra`` 字段会被信封透传，不需要为它单独维护一套异常体系。
    """

    def __init__(self, status_code: int, detail: Any, *, code: str | None = None,
                 headers: dict[str, str] | None = None, **extra: Any):
        super().__init__(status_code=status_code, detail=detail,
                         headers=headers)
        self.code = code
        self.extra = extra
