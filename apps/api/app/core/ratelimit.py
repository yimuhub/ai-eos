"""进程内滑动窗限流（V6.2 安全债：多租户防滥用第一层）。

单实例内存实现（滑动窗口计数），不用 Redis——LLM 网关本身是重操作，
内存桶足够拦住「脚本循环轰炸聊天」这类滥用；多实例部署时在网关层
（nginx/网关限流）做分布式版即可，语义保持一致。

用法（路由内一行）：
    from app.core.ratelimit import enforce_rate_limit
    enforce_rate_limit(f"chat:{user.id}", max_requests=20, window_seconds=60)

超限抛 HTTPException(429)，带 Retry-After 响应头。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException

_BUCKETS: dict[str, deque[float]] = defaultdict(deque)

# 缺省档位（可按端点覆写）
CHAT_STREAM_PER_MIN = 20   # 流式对话：每用户每分钟
CHAT_MSG_PER_MIN = 40      # 非流式对话：每用户每分钟
GROUP_SEND_PER_MIN = 30    # 群聊发言：每用户每分钟


def enforce_rate_limit(key: str, max_requests: int = CHAT_STREAM_PER_MIN,
                       window_seconds: int = 60) -> None:
    """滑动窗口计数；超限抛 429（带 Retry-After）。"""
    now = time.monotonic()
    bucket = _BUCKETS[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= max_requests:
        retry_after = max(1, int(window_seconds - (now - bucket[0])) + 1)
        raise HTTPException(
            429,
            f"操作太频繁（每分钟 {max_requests} 次上限），请 {retry_after} 秒后再试",
        )
    bucket.append(now)


def reset_rate_limit(key: str | None = None) -> None:
    """测试/管理用：清空指定桶（缺省全清）。"""
    if key is None:
        _BUCKETS.clear()
    else:
        _BUCKETS.pop(key, None)


def snapshot(key: str) -> int:
    """测试观察用：当前窗口内计数。"""
    now = time.monotonic()
    bucket = _BUCKETS[key]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    return len(bucket)
