"""AI Meeting System models (P2-04)."""
import enum

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class MeetingType(str, enum.Enum):
    DAILY_STANDUP = "DAILY_STANDUP"
    DEPARTMENT_MEETING = "DEPARTMENT_MEETING"
    PROJECT_MEETING = "PROJECT_MEETING"
    STRATEGY_MEETING = "STRATEGY_MEETING"
    REVIEW_MEETING = "REVIEW_MEETING"
    RETROSPECTIVE = "RETROSPECTIVE"
    EMERGENCY_MEETING = "EMERGENCY_MEETING"


class MeetingStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class DecisionStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    ADOPTED = "ADOPTED"
    REJECTED = "REJECTED"


class ActionItemStatus(str, enum.Enum):
    PENDING = "PENDING"          # 待转任务
    CONVERTED = "CONVERTED"      # 已转为任务
    DISMISSED = "DISMISSED"


class Meeting(TimestampMixin, Base):
    __tablename__ = "meetings"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    meeting_type: Mapped[str] = mapped_column(String(50), default=MeetingType.PROJECT_MEETING.value)
    status: Mapped[MeetingStatus] = mapped_column(Enum(MeetingStatus, native_enum=False, length=50), default=MeetingStatus.SCHEDULED, index=True)
    topic: Mapped[str | None] = mapped_column(Text)            # 议题
    context_material: Mapped[str | None] = mapped_column(Text)  # 会议材料/上下文
    minutes: Mapped[str | None] = mapped_column(Text)           # 纪要（Markdown）
    summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    coordinator_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    current_speaker_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))


class MeetingParticipant(TimestampMixin, Base):
    __tablename__ = "meeting_participants"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    role_note: Mapped[str | None] = mapped_column(String(100))   # 主持人/记录员等
    spoke_count: Mapped[int] = mapped_column(default=0)


class MeetingMessage(TimestampMixin, Base):
    __tablename__ = "meeting_messages"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    speaker_type: Mapped[str] = mapped_column(String(10))        # agent|user|system
    speaker_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    speaker_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    speaker_name: Mapped[str | None] = mapped_column(String(100))  # 冗余姓名，纪要用
    content: Mapped[str] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(String(30))        # opening|discussion|decision|closing


class MeetingDecision(TimestampMixin, Base):
    __tablename__ = "meeting_decisions"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[DecisionStatus] = mapped_column(
        Enum(DecisionStatus, native_enum=False, length=50), default=DecisionStatus.PROPOSED)
    proposed_by_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))


class MeetingActionItem(TimestampMixin, Base):
    __tablename__ = "meeting_action_items"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    assignee_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    status: Mapped[ActionItemStatus] = mapped_column(
        Enum(ActionItemStatus, native_enum=False, length=50), default=ActionItemStatus.PENDING)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))  # 转换后的任务
