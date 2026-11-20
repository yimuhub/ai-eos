"""人工介入手记（M4）：新增 `workflow_manual_log`，自主率账本的补充项。

纯新增表，存量执行不需要回填；没有手记时自主率只按节点类型计算。

Revision ID: 0033
Revises: 0032
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, Sequence[str], None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_manual_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("node_key", sa.String(length=50), nullable=True),
        sa.Column("reason", sa.String(length=20), nullable=False
                  , server_default="judgment"),
        sa.Column("minutes", sa.Float(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_manual_log_execution_id",
                    "workflow_manual_log", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_manual_log_execution_id",
                  table_name="workflow_manual_log")
    op.drop_table("workflow_manual_log")
