"""持久化后台任务队列。"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class QueueItemStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD = "DEAD"


class QueueItem(TimestampMixin, Base):
    __tablename__ = "queue_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # "task.execute" / "meeting.round" ...
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[QueueItemStatus] = mapped_column(
        Enum(QueueItemStatus, native_enum=False, length=20), nullable=False,
        default=QueueItemStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 幂等键：同一 company+kind+key 活跃期间不入队两次，由 service 层保证
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
