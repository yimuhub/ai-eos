"""失败请求必须进日志：404 未匹配路由 / 4xx / 5xx 的分级记录。

守的是一个真实的排障困境：用户在界面上看到一句 "Not Found"，而后端日志
里**一条记录都没有**——`uvicorn.access` 被压到 WARNING（避免刷屏），业务
层又不记 HTTP 状态，于是"前端调了后端不存在的地址"这类契约破坏完全静默。

这里钉死三件事：

1. 路由没匹配上的 404 → WARNING，且带上 request_id；
2. 正常业务 4xx（未登录等）→ INFO，不抢视野，但也不许彻底消失；
3. 日志行只记路径，**绝不带 query string**——query 里可能是密钥或令牌。
"""
from __future__ import annotations

import logging

import pytest

from app.main import _http_failure_level

pytestmark = pytest.mark.asyncio

_LOGGED = "app.main"


@pytest.fixture
def captured(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGED):
        yield caplog


async def test_unmatched_404_is_logged_as_warning_with_request_id(client, captured):
    resp = await client.get("/definitely-not-a-real-endpoint")
    assert resp.status_code == 404

    lines = [r.getMessage() for r in captured.records if "HTTP 失败" in r.getMessage()]
    assert len(lines) == 1, lines
    assert lines[0].startswith("HTTP 失败 GET /definitely-not-a-real-endpoint -> 404")
    assert "route=unmatched" in lines[0]

    warning = [r for r in captured.records
               if r.levelno == logging.WARNING and "HTTP 失败" in r.getMessage()]
    assert len(warning) == 1, warning
    # request_id 与响应头一致：用户报障时给一个 id 就能 grep 到这条
    assert f"request_id={resp.headers['X-Request-ID']}" in lines[0]


async def test_business_4xx_is_logged_at_info(client, captured):
    resp = await client.get("/companies")
    assert resp.status_code == 401

    lines = [r.getMessage() for r in captured.records if "HTTP 失败" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "route=" in lines[0] and "unmatched" not in lines[0]
    levels = {r.levelno for r in captured.records if "HTTP 失败" in r.getMessage()}
    assert levels == {logging.INFO}, levels


async def test_failure_log_never_contains_query_string(client, captured):
    await client.get("/nope?api_key=sk-should-not-appear&token=tail-me")

    joined = "\n".join(r.getMessage() for r in captured.records)
    assert "sk-should-not-appear" not in joined
    assert "tail-me" not in joined
    assert "/nope" in joined


@pytest.mark.parametrize("status,route_path,expected", [
    (500, "/tasks/{task_id}", logging.WARNING),
    (503, "unmatched", logging.WARNING),
    (404, "unmatched", logging.WARNING),
    (404, "/agents/{aid}", logging.INFO),
    (401, "/companies", logging.INFO),
    (422, "/tasks", logging.INFO),
])
async def test_failure_level_only_raises_for_invisible_failures(
        status, route_path, expected):
    assert _http_failure_level(status, route_path) == expected
