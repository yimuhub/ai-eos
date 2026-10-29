"""Unified event log system (P1-13)."""
from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class EventLog(TimestampMixin, Base):
    __tablename__ = "event_log"
    # created_at 来自 TimestampMixin，不能改 mixin（其它表共用），
    # 在这里单独为事件列表/导出的时间排序补索引。
    __table_args__ = (Index("ix_event_log_created_at", "created_at"),)

    event_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_type: Mapped[str | None] = mapped_column(String(20))  # system|user|agent
    actor_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 租户隔离键。历史行没有该值（NULL），查询侧一律不返回，安全优先。
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
