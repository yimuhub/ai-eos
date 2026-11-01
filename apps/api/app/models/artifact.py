"""Artifact Collaboration (P2-09): versioned deliverables with
Draft -> In Review -> Approved workflow.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ArtifactStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"      # 审核驳回，可修改后重新提交
    ARCHIVED = "ARCHIVED"


class Artifact(Base):
    """成果（逻辑实体）：指向最新版本。"""
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
        index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False,
                                                default="document")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, native_enum=False, length=50), nullable=False, default=ArtifactStatus.DRAFT,
        index=True)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
        index=True)
    accepted_by: Mapped[str | None] = mapped_column(String(36))
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)


class ArtifactVersion(Base):
    """成果版本：每次提交生成新版本，不可变。"""
    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False,
        index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    change_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, native_enum=False, length=50), nullable=False, default=ArtifactStatus.DRAFT)
    submitted_by: Mapped[str | None] = mapped_column(String(36))   # agent 或 user
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    review_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)


# 状态机：允许的流转
ALLOWED_TRANSITIONS = {
    ArtifactStatus.DRAFT: {ArtifactStatus.IN_REVIEW},
    ArtifactStatus.IN_REVIEW: {ArtifactStatus.APPROVED, ArtifactStatus.REJECTED,
                               ArtifactStatus.DRAFT},  # 可撤回
    ArtifactStatus.REJECTED: {ArtifactStatus.IN_REVIEW},  # 修改后重新提交
    ArtifactStatus.APPROVED: {
        ArtifactStatus.ARCHIVED,
        ArtifactStatus.REJECTED,   # B2 验收退回返工（不是审核驳回）
    },
    ArtifactStatus.ARCHIVED: set(),
}


def can_transition(current: ArtifactStatus, target: ArtifactStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())
