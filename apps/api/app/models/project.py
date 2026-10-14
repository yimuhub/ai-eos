"""Project Management System (P2-01)."""
import enum

from sqlalchemy import JSON, Date, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from .base import TimestampMixin


class ProjectStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"


class MilestoneStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False, length=50), default=ProjectStatus.DRAFT, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="NORMAL")
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    manager_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    start_date: Mapped[str | None] = mapped_column(String(20))
    target_date: Mapped[str | None] = mapped_column(String(20))

    milestones: Mapped[list["Milestone"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")


class Milestone(TimestampMixin, Base):
    __tablename__ = "milestones"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MilestoneStatus] = mapped_column(
        Enum(MilestoneStatus, native_enum=False, length=50), default=MilestoneStatus.PENDING)
    target_date: Mapped[str | None] = mapped_column(String(20))
    completed_at: Mapped[str | None] = mapped_column(String(50))

    project: Mapped["Project"] = relationship(back_populates="milestones")


class ProjectMember(TimestampMixin, Base):
    """Agent 或用户加入项目团队。"""
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "member_type", "member_id", name="uq_project_member"),
    )

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    member_type: Mapped[str] = mapped_column(String(10))  # agent | user
    member_id: Mapped[str] = mapped_column(String(36))
    role_in_project: Mapped[str | None] = mapped_column(String(100))

    project: Mapped["Project"] = relationship(back_populates="members")


class ProjectContextSnapshot(TimestampMixin, Base):
    """P2-08 预留：项目上下文快照（版本化）。"""
    __tablename__ = "project_context_snapshots"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    summary: Mapped[str | None] = mapped_column(Text)
    context_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
