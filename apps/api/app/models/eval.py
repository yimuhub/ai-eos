"""评测证据（M4）：一次"多 Agent 产出 vs 单次基线"的盲评记录。

rules 先说清楚，免得被当成营销数字：
- 盲评两次 A/B 顺序互换，结论一致才采纳，否则记 `tie`；
- 确定性维度（结论/引用/篇幅/结构/套话/缺件）先算，模型只补主观分；
- 没有模型评审时只按确定性分判，`judge` 字段为 null，标明"仅规则判定"。
"""
from sqlalchemy import Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class EvalRun(TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (Index("ix_evals_suite_case", "suite_key", "case_key"),)

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    suite_key: Mapped[str] = mapped_column(String(80))
    case_key: Mapped[str] = mapped_column(String(80))
    model_tier: Mapped[str] = mapped_column(String(40), default="unknown")
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    baseline_kind: Mapped[str] = mapped_column(String(20), default="single_agent")
    blind_order: Mapped[str] = mapped_column(String(10), default="A_first")
    rubric: Mapped[dict | None] = mapped_column(JSON, default=dict)
    scores: Mapped[dict | None] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(String(20), default="tie")  # multi_better|tie|baseline_better
    judge: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
