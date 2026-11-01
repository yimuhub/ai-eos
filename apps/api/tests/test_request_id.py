"""request_id 可观测性护栏：响应头、日志注入、500 兜底。

守的是一个具体的排障困境：用户报「报错了」时，日志里有几万行，
没有请求标识就只能靠时间戳猜是哪一次。这里把三件事钉死：

1. 每个响应（含 404 / 守卫提前返回）都带 X-Request-ID；
2. 合法透传、非法替换，避免日志被客户端塞进任意内容；
3. 500 兜底响应体里带同一个 request_id，日志记录里也带（无上下文时为 "-"）。
"""
from __future__ import annotations

import json
import logging
import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.core.logging_config import _LOG_FORMAT, RequestIdFilter
from app.core.request_context import (
    DEFAULT_REQUEST_ID,
    REQUEST_ID_HEADER,
    get_request_id,
    is_valid_request_id,
)
from app.main import _unhandled_exception_handler, request_id_middleware

_UUID4_HEX = re.compile(r"[0-9a-f]{32}")


def _log_record() -> logging.LogRecord:
    return logging.LogRecord("probe", logging.INFO, __file__, 1, "hello", None, None)


def _request_with_state(rid: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/boom",
        "raw_path": b"/boom",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "state": {"request_id": rid},
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# 响应头
# ---------------------------------------------------------------------------

async def test_response_gets_generated_request_id(client):
    r = await client.get("/health")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and _UUID4_HEX.fullmatch(rid), f"生成的 request_id 不合法：{rid!r}"


async def test_valid_client_request_id_is_echoed(client):
    supplied = "trace-abc_1.2"
    assert is_valid_request_id(supplied)
    r = await client.get("/health", headers={REQUEST_ID_HEADER: supplied})
    assert r.headers["x-request-id"] == supplied


@pytest.mark.parametrize("bad", [
    "short",              # 少于 8 位
    "x" * 129,            # 超过 128 位
    "has space inside",   # 非法字符
    "bad/slash-id",       # 非法字符（路径分隔符）
    "bad\nnewline-id",    # 换行注入（日志伪造）
])
async def test_invalid_request_id_is_replaced(client, bad):
    r = await client.get("/health", headers={REQUEST_ID_HEADER: bad})
    rid = r.headers["x-request-id"]
    assert rid != bad
    assert is_valid_request_id(rid)


async def test_404_also_carries_request_id(client):
    """未知路径由路由层返回 404，也必须能被日志关联。"""
    r = await client.get("/definitely-not-a-real-route-xyz")
    assert r.status_code == 404
    assert _UUID4_HEX.fullmatch(r.headers.get("x-request-id", ""))


async def test_cors_exposes_request_id(client):
    r = await client.get("/health", headers={"Origin": "http://localhost:3000"})
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in exposed


async def test_request_id_reaches_downstream_code():
    """中间件写入的 contextvar 在路由处理函数里可读（日志 Filter 同源）。"""
    app = FastAPI()
    app.middleware("http")(request_id_middleware)

    @app.get("/probe")
    async def probe():
        return {"rid": get_request_id()}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as c:
        r = await c.get("/probe", headers={REQUEST_ID_HEADER: "probe-12345"})

    assert r.json()["rid"] == "probe-12345"
    assert r.headers["x-request-id"] == "probe-12345"
    # 请求结束后上下文已还原：后台任务不会继承上一个请求的 id
    assert get_request_id() == DEFAULT_REQUEST_ID


# ---------------------------------------------------------------------------
# 日志注入
# ---------------------------------------------------------------------------

def test_log_filter_defaults_to_dash_without_request_context():
    """启动期/后台线程没有请求上下文：填 "-"，formatter 不能抛异常。"""
    record = _log_record()
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-"
    assert "[-]" in logging.Formatter(_LOG_FORMAT).format(record)


def test_log_filter_uses_current_request_id():
    from app.core.request_context import reset_request_id, set_request_id

    token = set_request_id("req-abcdef123")
    try:
        record = _log_record()
        RequestIdFilter().filter(record)
        assert record.request_id == "req-abcdef123"
        assert "[req-abcdef123]" in logging.Formatter(_LOG_FORMAT).format(record)
    finally:
        reset_request_id(token)


# ---------------------------------------------------------------------------
# 500 兜底
# ---------------------------------------------------------------------------

async def test_unhandled_exception_payload_contains_request_id():
    response = await _unhandled_exception_handler(
        _request_with_state("err-12345678"), RuntimeError("boom"))

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "err-12345678"
    body = json.loads(response.body)
    assert body["request_id"] == "err-12345678"
    # 原有字段必须保留：前端与日志排障都依赖它们
    assert body["error_type"] == "RuntimeError"
    assert "detail" in body and body["detail"]
