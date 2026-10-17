"""Agent Training System (P3-02).

Training 流程：Session → LLM 提取 Learning Points → Proposed Changes
→ 用户 Preview → Approve/Reject → Apply（更新 Profile/Knowledge/Memory）。
禁止直接修改 Identity / Core Safety Rules。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TrainingType(str, enum.Enum):
    CONVERSATION = "CONVERSATION"
    DOCUMENT = "DOCUMENT"
    EXAMPLE = "EXAMPLE"
    FEEDBACK = "FEEDBACK"
    TASK = "TASK"
    SKILL = "SKILL"
    MENTOR = "MENTOR"


class TrainingStatus(str, enum.Enum):
    PENDING = "PENDING"          # 已创建，待提取
    EXTRACTED = "EXTRACTED"      # 已提取 Learning Points，待用户确认
    APPROVED = "APPROVED"        # 用户批准
    REJECTED = "REJECTED"        # 用户拒绝
    APPLIED = "APPLIED"          # 变更已生效


class TrainingSession(Base):
    __tablename__ = "training_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    training_type: Mapped[TrainingType] = mapped_column(
        Enum(TrainingType, native_enum=False, length=50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_input: Mapped[str | None] = mapped_column(Text)          # 原始输入保存
    source_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)             # Training Summary
    status: Mapped[TrainingStatus] = mapped_column(
        Enum(TrainingStatus, native_enum=False, length=50), nullable=False, default=TrainingStatus.PENDING,
        index=True)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用


class TrainingLearningPoint(Base):
    """从训练内容提取的学习点。"""
    __tablename__ = "training_learning_points"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(30))  # knowledge/skill/preference/capability


class TrainingProposal(Base):
    """Proposed Changes：用户批准前不生效。"""
    __tablename__ = "training_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # knowledge_update / skill_improvement / preference_update / capability_update
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="PROPOSED")  # PROPOSED/APPROVED/REJECTED/APPLIED


class TrainingApplication(Base):
    """已应用的变更记录（可撤销）。"""
    __tablename__ = "training_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_proposals.id"), nullable=False)
    applied_changes: Mapped[dict | None] = mapped_column(JSON)   # 实际改动
    rollback_data: Mapped[dict | None] = mapped_column(JSON)     # 撤销所需的前置状态
    applied_at: Mapped[datetime] = mapped_column(default=_now)
    rolled_back_at: Mapped[datetime | None] = mapped_column(nullable=True)
