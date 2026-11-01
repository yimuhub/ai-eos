"""thread reads：会话已读跨设备同步（C3）

新表一张，老库升级后无需回填；唯一约束 `(user_id, company_id, thread_key)`
是合并写入的依据。只用到 VARCHAR / BIGINT / DATETIME / 外键与普通索引，
SQLite 与 PostgreSQL 通用。

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "thread_reads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("thread_key", sa.String(length=160), nullable=False),
        sa.Column("read_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "company_id", "thread_key",
                            name="uq_thread_reads_scope"),
    )
    op.create_index("ix_thread_reads_user_id", "thread_reads", ["user_id"])
    op.create_index("ix_thread_reads_company_id", "thread_reads",
                    ["company_id"])
    op.create_index("ix_thread_reads_user_company", "thread_reads",
                    ["user_id", "company_id"])


def downgrade() -> None:
    op.drop_table("thread_reads")
