"""Experience & Reflection Engine (P3-03).

任务完成后复盘：Task+Execution+Output+Errors+Feedback →
LLM Reflection → What Went Well / Failed / Why / Lessons / Improvement Proposal。
Reflection 不直接改 Agent，只产出 Improvement Proposal（供治理审批）。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReflectionTrigger(str, enum.Enum):
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"
    MANAGER_REQUESTED = "MANAGER_REQUESTED"
    HIGH_RISK = "HIGH_RISK"


class AgentExperience(Base):
    """工作经历（关联 Task/Project/Skill）。"""
    __tablename__ = "agent_experiences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS/FAILED/PARTIAL
    summary: Mapped[str | None] = mapped_column(Text)
    user_feedback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentReflection(Base):
    """Agent 复盘记录。"""
    __tablename__ = "agent_reflections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    experience_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_experiences.id", ondelete="CASCADE"),
        nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    trigger: Mapped[ReflectionTrigger] = mapped_column(
        Enum(ReflectionTrigger, native_enum=False, length=50), nullable=False, default=ReflectionTrigger.MANUAL)
    went_well: Mapped[list | None] = mapped_column(JSON)
    what_failed: Mapped[list | None] = mapped_column(JSON)
    root_cause: Mapped[str | None] = mapped_column(Text)
    lessons: Mapped[list | None] = mapped_column(JSON)  # Lessons Learned
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ExperienceLesson(Base):
    """经验教训（可独立检索，喂给后续任务的 prompt）。"""
    __tablename__ = "experience_lessons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    reflection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_reflections.id", ondelete="CASCADE"), nullable=True)
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list | None] = mapped_column(JSON)
    use_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ImprovementProposal(Base):
    """改进提案（需治理/用户批准，不自动生效）。"""
    __tablename__ = "improvement_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    reflection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_reflections.id", ondelete="CASCADE"),
        nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    proposal_type: Mapped[str | None] = mapped_column(String(30))
    # skill_add / skill_upgrade / knowledge_update / workflow_change
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="PROPOSED", index=True)
    # PROPOSED / APPROVED / REJECTED / APPLIED
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
