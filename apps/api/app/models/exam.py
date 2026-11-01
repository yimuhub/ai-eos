"""CEO 周考：由独立监督 Agent 出题、评分，老板可人工覆盖。

一周一场，按 ISO 周幂等。考试不只是打分，它要回答三件事：
1. 这位 CEO 现在是什么人格（进取/稳健/保守），有没有漂移；
2. 他在 Token 与成本控制上是否守纪律；
3. 经营决策是否可解释、可复算，而不是"听起来很对"。

题目、答案、评分、人格结论全部落库。人工覆盖不是"改分数"，而是留痕的
老板裁决：原分、覆盖人、覆盖时间、理由都保留，下一场周考仍从原分开始。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampMixin
from app.db.base import Base


class CeoExam(TimestampMixin, Base):
    __tablename__ = "ceo_exams"

    company_id: Mapped[str] = mapped_column(String(36), index=True)
    ceo_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    supervisor_agent_id: Mapped[str | None] = mapped_column(String(36),
                                                            nullable=True)
    # ISO 周，如 2026-W44；同公司同一周只允许一场
    period_key: Mapped[str] = mapped_column(String(20), index=True)
    week_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    week_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # PENDING → GRADED / SKIPPED / FAILED；老板覆盖后 → OVERRIDDEN
    status: Mapped[str] = mapped_column(String(20), default="PENDING",
                                        index=True)
    focus: Mapped[str | None] = mapped_column(String(500), nullable=True)
    questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    grades: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 维度分：personality / cost / decision / execution
    scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    personality: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(4), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 老板人工覆盖留痕：原分、覆盖人/时间/理由
    override: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        Index("ix_ceo_exams_company_period", "company_id", "period_key",
              unique=True),
    )
