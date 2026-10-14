"""Unified event log system (P1-13)."""
from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class EventLog(TimestampMixin, Base):
    __tablename__ = "event_log"

    event_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_type: Mapped[str | None] = mapped_column(String(20))  # system|user|agent
    actor_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
