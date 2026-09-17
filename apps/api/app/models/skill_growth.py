"""Skill Growth System models (P3-04).

在 P1 Skill 插件系统之上叠加成长维度：
- SkillNode: 技能树节点（父子/前置关系），独立于 P1 的插件 Skill
- AgentSkillNode: Agent 对某技能节点的掌握（等级+证据）
- SkillEvaluation: 等级评估记录（Evidence-based，禁止虚报）
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SkillLevel(str, enum.Enum):
    BEGINNER = "BEGINNER"
    JUNIOR = "JUNIOR"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"
    EXPERT = "EXPERT"


LEVEL_ORDER = {l.value: i for i, l in enumerate(SkillLevel)}


class SkillNode(Base):
    """技能树节点。"""
    __tablename__ = "skill_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                    index=True)  # NULL=全局
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("skill_nodes.id", ondelete="CASCADE"), nullable=True, index=True)
    prerequisite_id: Mapped[str | None] = mapped_column(
        ForeignKey("skill_nodes.id", ondelete="SET NULL"), nullable=True)
    prompt_template: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentSkillNode(Base):
    """Agent 的技能掌握记录（含证据）。"""
    __tablename__ = "agent_skill_nodes"
    __table_args__ = (UniqueConstraint("agent_id", "skill_node_id",
                                        name="uq_agent_skill_node"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    skill_node_id: Mapped[str] = mapped_column(
        ForeignKey("skill_nodes.id", ondelete="CASCADE"), nullable=False,
        index=True)
    level: Mapped[SkillLevel] = mapped_column(Enum(SkillLevel, native_enum=False, length=50), nullable=False,
                                               default=SkillLevel.BEGINNER)
    experience_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # [{"type":"task","ref":"task_id","note":"..."}, ...]
    source_type: Mapped[str | None] = mapped_column(String(30))  # assignment/training/evaluation/experience
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class SkillEvaluation(Base):
    """技能评估记录。"""
    __tablename__ = "skill_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    skill_node_id: Mapped[str] = mapped_column(
        ForeignKey("skill_nodes.id", ondelete="CASCADE"), nullable=False)
    previous_level: Mapped[SkillLevel | None] = mapped_column(
        Enum(SkillLevel, native_enum=False, length=50), nullable=True)
    new_level: Mapped[SkillLevel] = mapped_column(Enum(SkillLevel, native_enum=False, length=50), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str | None] = mapped_column(String(30))  # task_review/test/user_confirm
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    evaluated_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)


def new_id() -> str:
    return str(uuid.uuid4())
