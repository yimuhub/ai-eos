"""工具总线（V6 M1）：动态工具登记与 Agent 授权

新增 `tool_registry`（公司内动态工具：外部 MCP / 连接器 / 自定义）与
`agent_tool_grants`（按 Agent 的工具授权）。内置工具不落库（代码即事实源），
这两张表只承载"运行时可变"的部分。对应模型 `app/models/toolbus.py`。

Revision ID: 0019
Revises: 0018
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_registry",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default="custom"),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        sa.Column("package_key", sa.String(length=100), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("risk", sa.String(length=10), nullable=False,
                  server_default="low"),
        sa.Column("required_capability", sa.String(length=20), nullable=False,
                  server_default="operate"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False,
                  server_default="60"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("endpoint", sa.String(length=500), nullable=True),
        sa.Column("auth_secret_ref", sa.String(length=120), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("health_status", sa.String(length=20), nullable=True),
        sa.Column("last_health_at", sa.DateTime(), nullable=True),
        sa.Column("last_health_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", name="uq_tool_company_name"),
    )
    op.create_index("ix_tool_registry_company_id", "tool_registry",
                    ["company_id"])
    op.create_index("ix_tool_registry_name", "tool_registry", ["name"])
    op.create_index("ix_tool_registry_source", "tool_registry", ["source"])
    op.create_index("ix_tool_registry_package_key", "tool_registry",
                    ["package_key"])

    op.create_table(
        "agent_tool_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("granted_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "tool_name",
                            name="uq_tool_grant_agent"),
    )
    op.create_index("ix_agent_tool_grants_company_id", "agent_tool_grants",
                    ["company_id"])
    op.create_index("ix_agent_tool_grants_agent_id", "agent_tool_grants",
                    ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_tool_grants_agent_id", table_name="agent_tool_grants")
    op.drop_index("ix_agent_tool_grants_company_id",
                  table_name="agent_tool_grants")
    op.drop_table("agent_tool_grants")
    op.drop_index("ix_tool_registry_package_key", table_name="tool_registry")
    op.drop_index("ix_tool_registry_source", table_name="tool_registry")
    op.drop_index("ix_tool_registry_name", table_name="tool_registry")
    op.drop_index("ix_tool_registry_company_id", table_name="tool_registry")
    op.drop_table("tool_registry")
