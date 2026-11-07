"""UTC 时间的对外序列化。

SQLite 取出的 DateTime 不带时区（naive UTC）。直接 ``isoformat()`` 会得到
``2026-11-07T08:48:00``，浏览器 ``new Date()`` 按本地时区解析，东八区就差 8 小时。
所有回给前端的时刻统一走 ``iso_utc``，输出带 ``Z`` 的 ISO 串。
"""
from datetime import datetime, timezone


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")
