"""对外时刻统一带 `Z`：前端 `new Date()` 才不会把 UTC 当本地时间（东八区差 8 小时）。"""
from datetime import datetime, timedelta, timezone

from app.core.timeutil import iso_utc


def test_naive_datetime_is_treated_as_utc():
    assert iso_utc(datetime(2026, 11, 7, 8, 48)) == "2026-11-07T08:48:00Z"


def test_aware_datetime_is_normalized_to_utc():
    cst = timezone(timedelta(hours=8))
    assert iso_utc(datetime(2026, 11, 7, 16, 48, tzinfo=cst)) == "2026-11-07T08:48:00Z"


def test_none_stays_none():
    assert iso_utc(None) is None
