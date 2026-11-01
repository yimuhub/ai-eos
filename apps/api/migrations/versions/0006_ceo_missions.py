"""超级 Agent（CEO）：统筹准绳 + 执行台账（M1）

新表一张 `ceo_missions`；两张老表补可空列：`agents.is_ceo`（一个公司
至多一个负责人）与 `tasks.mission_id`（子任务归属哪场统筹）。全部可空，
老库升级后历史行保持 NULL，不需要回填；NULL 语义分别是"未指派 CEO"与
"不属于任何统筹"。

`tasks.mission_id` 不建外键：与 `parent_task_id` 保持一致——任务删除是
按公司级联清理的，跨表外键会让便携库上的删除顺序变脆；归属查询只按
索引过滤，不需要数据库层约束。

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ceo_missions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("parent_task_id", sa.String(length=36), nullable=True),
        sa.Column("coordinator_agent_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("deliverable", sa.Text(), nullable=True),
        sa.Column("redlines", sa.JSON(), nullable=False),
        sa.Column("acceptance", sa.JSON(), nullable=False),
        sa.Column("terminology", sa.JSON(), nullable=False),
        sa.Column("extra_instructions", sa.JSON(), nullable=False),
        sa.Column("anchor_version", sa.Integer(), nullable=False),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("effective_parallel", sa.Integer(), nullable=True),
        sa.Column("provider_cap", sa.Integer(), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=True),
        sa.Column("degrade_reason", sa.Text(), nullable=True),
        sa.Column("degraded_until", sa.String(length=50), nullable=True),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column("subtask_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("off_track_count", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("report_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.String(length=50), nullable=True),
        sa.Column("finished_at", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ceo_missions_company_id", "ceo_missions", ["company_id"])
    op.create_index("ix_ceo_missions_parent_task_id", "ceo_missions",
                    ["parent_task_id"])
    op.create_index("ix_ceo_missions_coordinator_agent_id", "ceo_missions",
                    ["coordinator_agent_id"])
    op.create_index("ix_ceo_missions_status", "ceo_missions", ["status"])
    op.create_index("ix_ceo_missions_company_status", "ceo_missions",
                    ["company_id", "status"])

    op.add_column("agents", sa.Column("is_ceo", sa.Boolean(), nullable=True))
    op.add_column("tasks", sa.Column("mission_id", sa.String(length=36),
                                     nullable=True))
    op.create_index("ix_tasks_mission_id", "tasks", ["mission_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_mission_id", table_name="tasks")
    op.drop_column("tasks", "mission_id")
    op.drop_column("agents", "is_ceo")

    op.drop_index("ix_ceo_missions_company_status", table_name="ceo_missions")
    op.drop_index("ix_ceo_missions_status", table_name="ceo_missions")
    op.drop_index("ix_ceo_missions_coordinator_agent_id", table_name="ceo_missions")
    op.drop_index("ix_ceo_missions_parent_task_id", table_name="ceo_missions")
    op.drop_index("ix_ceo_missions_company_id", table_name="ceo_missions")
    op.drop_table("ceo_missions")
