"""CEO 工作流生成与自优化：版本、评分、反馈表

新增 `workflow_feedback`：一次运行后的评分与经验，是下一版工作流的唯一输入。
老表补可空/有默认值的列：

* `workflow_definitions`：version/source/owner_agent_id/reuse_key/
  previous_workflow_id/score/run_count/success_count/failure_count/meta
* `ceo_missions.workflow_id`：本次统筹对应的（最新版）工作流

所有列都带默认值或可空，老数据无需回填。

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("mission_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default="ceo"),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("lessons", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_definitions.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_feedback_workflow_id", "workflow_feedback",
                    ["workflow_id"])
    op.create_index("ix_workflow_feedback_execution_id", "workflow_feedback",
                    ["execution_id"])
    op.create_index("ix_workflow_feedback_mission_id", "workflow_feedback",
                    ["mission_id"])

    with op.batch_alter_table("workflow_definitions") as batch_op:
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False,
                                      server_default="1"))
        batch_op.add_column(sa.Column("source", sa.String(length=20),
                                      nullable=False, server_default="manual"))
        batch_op.add_column(sa.Column("owner_agent_id", sa.String(length=36),
                                      nullable=True))
        batch_op.add_column(sa.Column("reuse_key", sa.String(length=200),
                                      nullable=True))
        batch_op.add_column(sa.Column("previous_workflow_id",
                                      sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("run_count", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("success_count", sa.Integer(),
                                      nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("failure_count", sa.Integer(),
                                      nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("meta", sa.JSON(), nullable=True))
        batch_op.create_index("ix_workflow_definitions_owner_agent_id",
                              ["owner_agent_id"])
        batch_op.create_index("ix_workflow_definitions_reuse_key",
                              ["reuse_key"])

    with op.batch_alter_table("ceo_missions") as batch_op:
        batch_op.add_column(sa.Column("workflow_id", sa.String(length=36),
                                      nullable=True))
        batch_op.create_index("ix_ceo_missions_workflow_id", ["workflow_id"])


def downgrade() -> None:
    with op.batch_alter_table("ceo_missions") as batch_op:
        batch_op.drop_index("ix_ceo_missions_workflow_id")
        batch_op.drop_column("workflow_id")

    with op.batch_alter_table("workflow_definitions") as batch_op:
        batch_op.drop_index("ix_workflow_definitions_reuse_key")
        batch_op.drop_index("ix_workflow_definitions_owner_agent_id")
        batch_op.drop_column("meta")
        batch_op.drop_column("failure_count")
        batch_op.drop_column("success_count")
        batch_op.drop_column("run_count")
        batch_op.drop_column("score")
        batch_op.drop_column("previous_workflow_id")
        batch_op.drop_column("reuse_key")
        batch_op.drop_column("owner_agent_id")
        batch_op.drop_column("source")
        batch_op.drop_column("version")

    op.drop_index("ix_workflow_feedback_mission_id",
                  table_name="workflow_feedback")
    op.drop_index("ix_workflow_feedback_execution_id",
                  table_name="workflow_feedback")
    op.drop_index("ix_workflow_feedback_workflow_id",
                  table_name="workflow_feedback")
    op.drop_table("workflow_feedback")
