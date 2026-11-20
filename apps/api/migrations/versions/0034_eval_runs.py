"""评测证据（M4）：新增 `eval_runs`，一次盲评一行。

纯新增表；没有评测记录时胜率返回 null（不把"没数据"写成 0%）。

Revision ID: 0034
Revises: 0033
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, Sequence[str], None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("suite_key", sa.String(length=80), nullable=False),
        sa.Column("case_key", sa.String(length=80), nullable=False),
        sa.Column("model_tier", sa.String(length=40), nullable=False,
                  server_default="unknown"),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("baseline_kind", sa.String(length=20), nullable=False,
                  server_default="single_agent"),
        sa.Column("blind_order", sa.String(length=10), nullable=False,
                  server_default="A_first"),
        sa.Column("rubric", sa.JSON(), nullable=True),
        sa.Column("scores", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(length=20), nullable=False,
                  server_default="tie"),
        sa.Column("judge", sa.JSON(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eval_runs_company_id", "eval_runs", ["company_id"])
    op.create_index("ix_evals_suite_case", "eval_runs", ["suite_key", "case_key"])


def downgrade() -> None:
    op.drop_index("ix_evals_suite_case", table_name="eval_runs")
    op.drop_index("ix_eval_runs_company_id", table_name="eval_runs")
    op.drop_table("eval_runs")
