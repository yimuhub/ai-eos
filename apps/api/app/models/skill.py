"""Skill plugin system (P1-06)."""
import enum

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class SkillStatus(str, enum.Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class Skill(TimestampMixin, Base):
    """Registered skill plugin manifest."""
    __tablename__ = "skills"

    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(20), default="0.1.0")
    input_schema: Mapped[dict | None] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict | None] = mapped_column(JSON, default=dict)
    permissions: Mapped[list | None] = mapped_column(JSON, default=list)
    status: Mapped[SkillStatus] = mapped_column(Enum(SkillStatus, native_enum=False, length=50), default=SkillStatus.ENABLED)
    scope: Mapped[str] = mapped_column(String(10), default="public")  # public=公共 | exclusive=专属
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # NULL=系统内置
    source: Mapped[str] = mapped_column(String(20), default="builtin")  # builtin|custom|imported


class AgentSkill(TimestampMixin, Base):
    __tablename__ = "agent_skills"
    __table_args__ = (UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    # 绑定来源：manual=手工安装，auto=自动配备，ceo_bundle=CEO 专属包。
    # 解除 CEO 时只删 ceo_bundle，绝不误删老板手工装或员工自己学的技能。
    # 可空是为了兼容升级前的老行（NULL 等价 manual）。
    source: Mapped[str | None] = mapped_column(String(20), nullable=True,
                                               default="manual")
