"""Career System + Promotion/Transfer (P3-05 & P3-06).

Career: 职位路径 + Agent 任职履历（append-only）。
Promotion/Transfer: Gap分析 → 训练计划 → 试岗 → 提案 → 人工批准。
禁止系统自动改变 Agent 岗位。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CareerPosition(Base):
    """职位定义（含晋升路径层级）。"""
    __tablename__ = "career_positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                    index=True)  # NULL=全局模板
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 越大越高
    department: Mapped[str | None] = mapped_column(String(100))
    required_skill_node_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    next_position_id: Mapped[str | None] = mapped_column(
        ForeignKey("career_positions.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentCareerRecord(Base):
    """Agent 任职履历（append-only）。"""
    __tablename__ = "agent_career_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    position_id: Mapped[str] = mapped_column(
        ForeignKey("career_positions.id"), nullable=False)
    position_title: Mapped[str] = mapped_column(String(100), nullable=False)
    department: Mapped[str | None] = mapped_column(String(100))
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # JOINED / PROMOTION / TRANSFER / TEMPORARY / ENDED
    reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(36))
    start_date: Mapped[datetime] = mapped_column(default=_now)
    end_date: Mapped[datetime | None] = mapped_column(nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TrialAssignment(Base):
    """试岗。"""
    __tablename__ = "trial_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    target_position_id: Mapped[str] = mapped_column(
        ForeignKey("career_positions.id"), nullable=False)
    task_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="IN_PROGRESS")
    # IN_PROGRESS / PASSED / FAILED
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CareerProposal(Base):
    """晋升/转岗提案（人工批准后才生效）。"""
    __tablename__ = "career_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    proposal_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # PROMOTION / TRANSFER / DUAL_ROLE / TEMPORARY
    from_position_id: Mapped[str | None] = mapped_column(
        ForeignKey("career_positions.id"))
    to_position_id: Mapped[str] = mapped_column(
        ForeignKey("career_positions.id"), nullable=False)
    gap_analysis: Mapped[dict | None] = mapped_column(JSON)  # 缺口分析
    training_plan: Mapped[dict | None] = mapped_column(JSON)  # 训练计划
    trial_id: Mapped[str | None] = mapped_column(
        ForeignKey("trial_assignments.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="PROPOSED", index=True)
    # DRAFT / PROPOSED / APPROVED / REJECTED / APPLIED
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
