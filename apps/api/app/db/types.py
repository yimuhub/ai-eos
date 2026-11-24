from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class CalendarDateTime(TypeDecorator[datetime]):
    """Store calendar boundaries as wall time, consistently across dialects."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None,
                           dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        wall_time = value.replace(tzinfo=None)
        if dialect.name == "sqlite":
            return wall_time
        return wall_time.replace(tzinfo=timezone.utc)

    def process_result_value(self, value: datetime | None,
                             dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value
