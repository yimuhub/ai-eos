"""V5.7 ⑥⑦：下班学习（Letta 风格）+ 高置信沉淀（mem0 风格）。

- AgentWorklog：员工空转期写的"工作日志/经验"（ADD-only 红线：只追加，
  不覆盖历史；老板审批通过后沉淀为高置信记忆）。
- 日志变更/沉淀走老板审批门（成长也要老板点头——确认点哲学）。
- 沉淀结果写入 AgentMemory，importance=0.95，来源标注 source_ref。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import utcnow


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WorklogStatus(str, enum.Enum):
    PENDING = "PENDING"      # 员工写好，等老板批
    APPROVED = "APPROVED"    # 老板批准 → 已沉淀为高置信记忆
    REJECTED = "REJECTED"    # 老板否决（保留记录，标注理由）


class AgentWorklog(Base):
    """V5.7 ⑥ 下班学习日志（ADD-only，可审计）。"""
    __tablename__ = "agent_worklogs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(20), default="daily")  # daily/weekly
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    lessons: Mapped[list | None] = mapped_column(JSON)  # 经验点列表
    task_refs: Mapped[list | None] = mapped_column(JSON)  # 关联任务/执行
    status: Mapped[WorklogStatus] = mapped_column(
        Enum(WorklogStatus, native_enum=False, length=20),
        nullable=False, default=WorklogStatus.PENDING)
    memory_ref: Mapped[str | None] = mapped_column(String(36))  # 沉淀后的 AgentMemory id
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
