"""Agent Learning Center (P3-01).

每个 Agent 的独立成长档案。核心原则：
- Identity/Role/Capability/Knowledge/Experience/Memory/Preference 独立管理
- 任何 Role/Skill/Capability Change 必须生成 Growth Event
- 成长历史只追加、不覆盖（append-only）
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GrowthEventType(str, enum.Enum):
    CREATED = "CREATED"
    ROLE_CHANGED = "ROLE_CHANGED"
    SKILL_ADDED = "SKILL_ADDED"
    SKILL_UPGRADED = "SKILL_UPGRADED"
    SKILL_REMOVED = "SKILL_REMOVED"
    CAPABILITY_CHANGED = "CAPABILITY_CHANGED"
    KNOWLEDGE_UPDATED = "KNOWLEDGE_UPDATED"
    TRAINED = "TRAINED"
    REFLECTED = "REFLECTED"
    PROMOTED = "PROMOTED"
    TRANSFERRED = "TRANSFERRED"
    EVALUATED = "EVALUATED"
    MODEL_CHANGED = "MODEL_CHANGED"


class AgentProfile(Base):
    """Agent 成长档案主表（1:1 Agent）。"""
    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), unique=True,
        nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    identity: Mapped[str] = mapped_column(String(255), nullable=False)  # 身份本质，不可被训练改变
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    learning_goals: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class AgentGrowthEvent(Base):
    """成长事件（append-only，可追溯）。"""
    __tablename__ = "agent_growth_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[GrowthEventType] = mapped_column(
        Enum(GrowthEventType, native_enum=False, length=50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # before/after
    source_type: Mapped[str | None] = mapped_column(String(30))  # training/reflection/manual/system
    source_id: Mapped[str | None] = mapped_column(String(36))
    actor: Mapped[str | None] = mapped_column(String(36))  # user 或 agent
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)


class AgentLearningRecord(Base):
    """学习计划/学习记录。"""
    __tablename__ = "agent_learning_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PLANNED",
                                         index=True)  # PLANNED/IN_PROGRESS/DONE/ABANDONED
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class AgentCapabilityHistory(Base):
    """能力快照历史（版本化，禁止覆盖）。"""
    __tablename__ = "agent_capability_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    snapshot_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
