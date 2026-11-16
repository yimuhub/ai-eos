"""自然人 ↔ 数字员工绑定（V6 M5 人机组织模型）

新增 `member_agent_bindings`：user ↔ agent 的归属关系（assistant/
subordinate/consultant），支撑「每个自然人拥有多个 Agent、老板拥有军团」。
对应模型 `app/models/bindings.py`。

Revision ID: 0023
Revises: 0022
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, Sequence[str], None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_agent_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=20), nullable=False,
                  server_default="subordinate"),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "agent_id", name="uq_member_agent"),
    )
    op.create_index("ix_member_agent_bindings_company_id",
                    "member_agent_bindings", ["company_id"])
    op.create_index("ix_member_agent_bindings_user_id",
                    "member_agent_bindings", ["user_id"])
    op.create_index("ix_member_agent_bindings_agent_id",
                    "member_agent_bindings", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_member_agent_bindings_agent_id",
                  table_name="member_agent_bindings")
    op.drop_index("ix_member_agent_bindings_user_id",
                  table_name="member_agent_bindings")
    op.drop_index("ix_member_agent_bindings_company_id",
                  table_name="member_agent_bindings")
    op.drop_table("member_agent_bindings")
