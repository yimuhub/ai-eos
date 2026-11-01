"""schema foundation: 后台队列 / 交付评论 / 订阅试用 / BYOK 密钥库

新表 4 张（queue_items / deliverable_comments / company_subscriptions /
company_secrets），并给 artifacts 补验收字段、给 conversations 补 project_id。
全部新增列可空，老库升级后历史行保持 NULL，不需要回填。

Revision ID: c8a71f5d3e42
Revises: b2d4f6a8c1e3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8a71f5d3e42"
down_revision: Union[str, Sequence[str], None] = "b2d4f6a8c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "queue_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum(
            "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "DEAD",
            name="queueitemstatus", native_enum=False, length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_queue_items_company_id", "queue_items", ["company_id"])
    op.create_index("ix_queue_items_kind", "queue_items", ["kind"])
    op.create_index("ix_queue_items_status", "queue_items", ["status"])
    op.create_index("ix_queue_items_available_at", "queue_items", ["available_at"])
    op.create_index("ix_queue_items_lease_expires_at", "queue_items",
                    ["lease_expires_at"])
    op.create_index("ix_queue_items_dedupe_key", "queue_items", ["dedupe_key"])

    op.create_table(
        "deliverable_comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("author_type", sa.String(length=20), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deliverable_comments_company_id", "deliverable_comments",
                    ["company_id"])
    op.create_index("ix_deliverable_comments_artifact_id", "deliverable_comments",
                    ["artifact_id"])

    op.create_table(
        "company_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("plan_code", sa.String(length=40), nullable=False),
        sa.Column("status", sa.Enum(
            "TRIALING", "ACTIVE", "PAST_DUE", "CANCELED",
            name="subscriptionstatus", native_enum=False, length=20), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("seats_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_token_quota", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_subscriptions_company_id", "company_subscriptions",
                    ["company_id"], unique=True)

    op.create_table(
        "company_secrets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", name="uq_company_secret_name"),
    )
    op.create_index("ix_company_secrets_company_id", "company_secrets",
                    ["company_id"])

    # SQLite 不支持 ALTER 加约束：带外键的新列必须走 batch（重建表）路径。
    # 新约束必须显式命名，否则 batch 重建时 Alembic 拒绝生成 DDL。
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("task_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column(
            "accepted_by", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("accepted_at", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            "fk_artifacts_task_id", "tasks", ["task_id"], ["id"],
            ondelete="SET NULL")
        batch_op.create_index("ix_artifacts_task_id", ["task_id"])

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column(
            "project_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_conversations_project_id", "projects", ["project_id"], ["id"],
            ondelete="SET NULL")
        batch_op.create_index("ix_conversations_project_id", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    # SQLite 的 DROP COLUMN 依赖版本，batch 模式统一走"重建表"路径
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("project_id")

    op.drop_index("ix_artifacts_task_id", table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("accepted_at")
        batch_op.drop_column("accepted_by")
        batch_op.drop_column("task_id")

    op.drop_table("company_secrets")
    op.drop_table("company_subscriptions")
    op.drop_table("deliverable_comments")
    op.drop_table("queue_items")
