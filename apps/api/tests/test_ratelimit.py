"""每租户限流（V6.2 安全债）——钉住契约：

1. 滑动窗口计数：窗口内第 N+1 次抛 429（带人话文案），出窗恢复；
2. 键隔离：不同用户互不影响；
3. chat / chat-stream / group 三类端点都挂了限流（静态扫描钉住）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.ratelimit import (CHAT_MSG_PER_MIN, CHAT_STREAM_PER_MIN,
                                enforce_rate_limit, reset_rate_limit,
                                snapshot)

ROUTES_DIR = Path(__file__).resolve().parents[1] / "app" / "api" / "routes"


def test_sliding_window_blocks_at_limit():
    reset_rate_limit("u1")
    for _ in range(3):
        enforce_rate_limit("u1", max_requests=3, window_seconds=60)
    assert snapshot("u1") == 3
    with pytest.raises(HTTPException) as exc:
        enforce_rate_limit("u1", max_requests=3, window_seconds=60)
    assert exc.value.status_code == 429
    assert "太频繁" in str(exc.value.detail)


def test_keys_are_isolated():
    reset_rate_limit("uA")
    reset_rate_limit("uB")
    for _ in range(3):
        enforce_rate_limit("uA", max_requests=3, window_seconds=60)
    # uB 没打过卡：不受 uA 影响
    enforce_rate_limit("uB", max_requests=3, window_seconds=60)
    assert snapshot("uB") == 1


def test_window_expiry(monkeypatch):
    reset_rate_limit("uW")
    now = {"t": 1000.0}
    monkeypatch.setattr("app.core.ratelimit.time.monotonic", lambda: now["t"])
    for _ in range(2):
        enforce_rate_limit("uW", max_requests=2, window_seconds=30)
    with pytest.raises(HTTPException):
        enforce_rate_limit("uW", max_requests=2, window_seconds=30)
    # 时间滑出窗口：计数归零，恢复可用
    now["t"] += 31
    enforce_rate_limit("uW", max_requests=2, window_seconds=30)
    assert snapshot("uW") == 1


def test_chat_endpoints_are_rate_limited():
    """静态钉住：三类聊天端点的函数体里必须出现 enforce_rate_limit。"""
    agent_chat = (ROUTES_DIR / "agent_chat.py").read_text(encoding="utf-8")
    group_chat = (ROUTES_DIR / "group_chat.py").read_text(encoding="utf-8")
    for fn in ("async def boss_chat(", "async def boss_chat_stream("):
        block = _extract_function(agent_chat, fn)
        assert "enforce_rate_limit" in block, f"{fn} 缺限流"
    for fn in ("async def group_chat(", "async def group_chat_stream("):
        block = _extract_function(group_chat, fn)
        assert "enforce_rate_limit" in block, f"{fn} 缺限流"
    assert CHAT_STREAM_PER_MIN <= 30 and CHAT_MSG_PER_MIN <= 60


def _extract_function(src: str, header: str) -> str:
    i = src.find(header)
    assert i >= 0, header
    nxt = src.find("\n@router", i + 1)
    nxt2 = src.find("\nasync def", i + 10)
    ends = [x for x in (nxt, nxt2) if x > 0]
    return src[i:min(ends)] if ends else src[i:i + 3000]
