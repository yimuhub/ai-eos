"""Shared utilities (refactor: eliminate _now/new_id duplication across 25 files)."""
import uuid
from datetime import datetime, timezone


def now_naive() -> datetime:
    """Naive UTC datetime — DB 列均为 TIMESTAMP WITHOUT TIME ZONE。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())
