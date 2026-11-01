"""push subscriptions：Web Push（C6）

移动端 /m 的离线推送订阅表。新表 + 可空列，老库升级后无需回填。
SQLite / PostgreSQL 通用：只用到 VARCHAR / DATETIME / 外键与普通索引，
不依赖方言特性；``endpoint`` 唯一索引即订阅 upsert 的依据。

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint", sa.String(length=2000), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=128), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_push_subscriptions_company_id", "push_subscriptions",
                    ["company_id"])
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions",
                    ["user_id"])
    op.create_index("ix_push_subscriptions_endpoint", "push_subscriptions",
                    ["endpoint"], unique=True)


def downgrade() -> None:
    op.drop_table("push_subscriptions")
