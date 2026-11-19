"""敏感访问台账（V6.2 系统日志功能）

新增 `sensitive_access`：谁（管理员）在什么时间、以什么理由、通过什么
验证方式查看了哪类敏感内容（如 Agent 聊天记录）。每次查看一行——
这是「看敏感内容必留痕」的台账本体，供超级管理后台的审计页直接展示。

Revision ID: 0027
Revises: 0026
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, Sequence[str], None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sensitive_access",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("admin_user_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=120), nullable=False),
        sa.Column("target_desc", sa.String(length=300), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("auth", sa.String(length=20), nullable=False,
                  server_default="totp"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sensitive_access_company_id", "sensitive_access",
                    ["company_id"])
    op.create_index("ix_sensitive_access_admin_user_id", "sensitive_access",
                    ["admin_user_id"])
    op.create_index("ix_sensitive_access_category", "sensitive_access",
                    ["category"])
    op.create_index("ix_sensitive_access_created_at", "sensitive_access",
                    ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_sensitive_access_category", table_name="sensitive_access")
    op.drop_index("ix_sensitive_access_admin_user_id",
                  table_name="sensitive_access")
    op.drop_index("ix_sensitive_access_company_id", table_name="sensitive_access")
    op.drop_table("sensitive_access")
