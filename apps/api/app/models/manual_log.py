"""人工介入手记（M4）：自主率账本的唯一"人工侧"补充。

口径按**次数**不按工作量——一次介入就是一次，不折算工时。
reason 四选一，用来回答"AI 卡在哪一类事情上"：
- unsupported：系统做不到
- quality：产出质量不合格，人工重做
- judgment：需要人拍板（价值判断、对外口径）
- external：外部系统/人不到位（等资料、等对方确认）
"""
from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin

MANUAL_REASONS = ("unsupported", "quality", "judgment", "external")


class WorkflowManualLog(TimestampMixin, Base):
    __tablename__ = "workflow_manual_log"

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str | None] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(String(20), default="judgment")
    minutes: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))
