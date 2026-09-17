"""Agent Evaluation & Review (P3-07).

多源考核：SELF/PEER/MANAGER/USER_REVIEW + TASK_EVALUATION +
AUTOMATED_EVALUATION（基于真实执行数据，禁止模型编造）。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReviewSource(str, enum.Enum):
    SELF = "SELF"
    PEER = "PEER"
    MANAGER = "MANAGER"
    USER = "USER"
    TASK_EVALUATION = "TASK_EVALUATION"
    AUTOMATED = "AUTOMATED"


class AgentReview(Base):
    __tablename__ = "agent_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source: Mapped[ReviewSource] = mapped_column(Enum(ReviewSource, native_enum=False, length=50), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)   # 0-100
    strengths: Mapped[list | None] = mapped_column(JSON)
    weaknesses: Mapped[list | None] = mapped_column(JSON)
    suggestions: Mapped[list | None] = mapped_column(JSON)
    evidence: Mapped[dict | None] = mapped_column(JSON)  # automated 用真实指标
    comment: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str | None] = mapped_column(String(36))  # user/agent id
    cycle_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ReviewCycle(Base):
    """考核周期（如月度考核）。"""
    __tablename__ = "review_cycles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(default=_now)
