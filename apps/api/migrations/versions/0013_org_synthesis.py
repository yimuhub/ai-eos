"""自动组织架构生成 run

新增 `org_synthesis_runs`：一次「资料 → 画像 → 组织设计 → 审查 → 落地」
的全过程记录。表结构只存快照与草稿（JSON），不放业务规则，
规则留在 `app/services/org_synthesis.py`，后续调整无需再改表。

Revision ID: 0013
Revises: 0012
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_synthesis_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("stages", sa.JSON(), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_org_synthesis_runs_company_id",
                    "org_synthesis_runs", ["company_id"])
    op.create_index("ix_org_synthesis_runs_status",
                    "org_synthesis_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_org_synthesis_runs_status",
                  table_name="org_synthesis_runs")
    op.drop_index("ix_org_synthesis_runs_company_id",
                  table_name="org_synthesis_runs")
    op.drop_table("org_synthesis_runs")
