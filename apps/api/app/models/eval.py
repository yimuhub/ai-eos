"""Evaluation records: manual rules-only evidence and server-owned verified runs.

Legacy/manual rows may have no idempotency key. Real workflow runs use a unique
key over company + suite + case + source execution + content fingerprint.
"""
from datetime import datetime

from sqlalchemy import (DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class EvalRun(TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_evals_suite_case", "suite_key", "case_key"),
        UniqueConstraint("idempotency_key", name="uq_eval_runs_idempotency_key"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    suite_key: Mapped[str] = mapped_column(String(80))
    case_key: Mapped[str] = mapped_column(String(80))
    model_tier: Mapped[str] = mapped_column(String(40), default="unknown")
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    baseline_kind: Mapped[str] = mapped_column(String(20), default="single_agent")
    blind_order: Mapped[str] = mapped_column(String(10), default="none")
    rubric: Mapped[dict | None] = mapped_column(JSON, default=dict)
    scores: Mapped[dict | None] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(String(20), default="unavailable")
    judge: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # F07: nullable so portable upgrades preserve every historical row.
    run_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    verification: Mapped[str | None] = mapped_column(String(30), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    blind_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    baseline_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    multi_output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True)
    rubric_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    judge_model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    judge_model_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    judge_prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Worker lease + immutable-attempt lineage. A retry is a new row, never a
    # rewrite of terminal evidence.
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
