"""F07 evaluation evidence, async state and idempotency.

Revision ID: 0037
Revises: 0036
"""
from typing import Sequence, Union
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0037"
down_revision: Union[str, Sequence[str], None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = [
        sa.Column("run_kind", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("verification", sa.String(length=30), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("blind_evidence", sa.JSON(), nullable=True),
        sa.Column("source_execution_id", sa.String(length=36), nullable=True),
        sa.Column("baseline_task_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("workflow_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("multi_output_hash", sa.String(length=64), nullable=True),
        sa.Column("baseline_output_hash", sa.String(length=64), nullable=True),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("rubric_version", sa.String(length=80), nullable=True),
        sa.Column("judge_model_id", sa.String(length=36), nullable=True),
        sa.Column("judge_model_hash", sa.String(length=64), nullable=True),
        sa.Column("judge_prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("retry_of", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in columns:
        op.add_column("eval_runs", column)

    op.create_index("ix_eval_runs_status", "eval_runs", ["status"])
    op.create_index("ix_eval_runs_source_execution_id", "eval_runs",
                    ["source_execution_id"])
    op.create_index("ix_eval_runs_content_fingerprint", "eval_runs",
                    ["content_fingerprint"])
    # batch mode is required for a unique constraint on SQLite. NULL remains
    # allowed, so all historical/manual records survive unchanged.
    with op.batch_alter_table("eval_runs") as batch_op:
        batch_op.create_unique_constraint("uq_eval_runs_idempotency_key",
                                          ["idempotency_key"])

    _ensure_workflow_step_ids()


def _ensure_workflow_step_ids() -> None:
    """Give every historical step a stable UUID before enforcing a primary key.

    Some early portable databases predate the model's inherited IdMixin and
    therefore have no id column at all. Adding a non-null UUID default directly
    is not portable (and one shared default would violate uniqueness), so backfill
    row by row, then rebuild SQLite tables through Alembic's batch mode.
    """
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("workflow_steps")}
    added = "id" not in columns
    has_primary_key = inspector.get_pk_constraint("workflow_steps").get(
        "constrained_columns") == ["id"]
    if added:
        op.add_column("workflow_steps", sa.Column("id", sa.String(length=36),
                                                  nullable=True))
    rows = bind.execute(sa.text(
        "SELECT rowid AS legacy_rowid FROM workflow_steps WHERE id IS NULL"
    ) if bind.dialect.name == "sqlite" else sa.text(
        "SELECT ctid AS legacy_rowid FROM workflow_steps WHERE id IS NULL"
    )).scalars().all()
    for legacy_rowid in rows:
        bind.execute(sa.text(
            "UPDATE workflow_steps SET id=:id WHERE rowid=:rowid"
            if bind.dialect.name == "sqlite" else
            "UPDATE workflow_steps SET id=:id WHERE ctid=:rowid"
        ), {"id": str(uuid.uuid4()), "rowid": legacy_rowid})
    if not has_primary_key:
        with op.batch_alter_table("workflow_steps") as batch_op:
            batch_op.alter_column("id", existing_type=sa.String(length=36),
                                  nullable=False)
            batch_op.create_primary_key("pk_workflow_steps", ["id"])


def downgrade() -> None:
    with op.batch_alter_table("eval_runs") as batch_op:
        batch_op.drop_constraint("uq_eval_runs_idempotency_key", type_="unique")
    op.drop_index("ix_eval_runs_content_fingerprint", table_name="eval_runs")
    op.drop_index("ix_eval_runs_source_execution_id", table_name="eval_runs")
    op.drop_index("ix_eval_runs_status", table_name="eval_runs")
    for name in ("run_kind", "status", "verification", "evidence",
                 "blind_evidence", "source_execution_id", "baseline_task_id",
                 "prompt_hash", "workflow_snapshot_hash", "multi_output_hash",
                 "baseline_output_hash", "content_fingerprint", "idempotency_key",
                 "rubric_version", "judge_model_id", "judge_model_hash",
                 "judge_prompt_hash", "error", "attempt", "retry_of",
                 "worker_id", "lease_expires_at"):
        op.drop_column("eval_runs", name)
