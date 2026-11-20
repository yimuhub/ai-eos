"""步骤级验收闸门（M1）

新增 `workflow_step_gates`：一个节点每过一次闸门写一行，记录哪条断言没过、
实际值多少、评审分数与 token 用量。只建表，不改任何既有表——上线可回滚，
存量数据零影响。

Revision ID: 0030
Revises: 0029
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, Sequence[str], None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_step_gates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("node_key", sa.String(length=50), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("gate_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(length=20), nullable=False,
                  server_default="mechanical"),
        sa.Column("checks", sa.JSON(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("judge_model", sa.String(length=120), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_before", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_step_gates_execution_id", "workflow_step_gates",
                    ["execution_id"])
    op.create_index("ix_wfgates_exec_node", "workflow_step_gates",
                    ["execution_id", "node_key"])
    op.create_index("ix_wfgates_passed", "workflow_step_gates", ["passed"])


def downgrade() -> None:
    op.drop_index("ix_wfgates_passed", table_name="workflow_step_gates")
    op.drop_index("ix_wfgates_exec_node", table_name="workflow_step_gates")
    op.drop_index("ix_workflow_step_gates_execution_id",
                  table_name="workflow_step_gates")
    op.drop_table("workflow_step_gates")
