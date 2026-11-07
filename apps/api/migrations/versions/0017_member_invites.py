"""成员邀请链接

新增 `member_invites`：公司管理员生成一次性链接，被邀请人自己登录/注册后
凭链接加入。库中只存 token 的 SHA-256，明文只在创建响应里出现一次。

Revision ID: 0017
Revises: 0016
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_invites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("department_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_by", sa.String(length=36), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_member_invites_company_id",
                    "member_invites", ["company_id"])
    op.create_index("ix_member_invites_token_hash",
                    "member_invites", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_member_invites_token_hash",
                  table_name="member_invites")
    op.drop_index("ix_member_invites_company_id",
                  table_name="member_invites")
    op.drop_table("member_invites")
