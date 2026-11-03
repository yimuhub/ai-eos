"""插件登记与本机操作策略

新增 `plugin_installations`：按公司记录本机已装插件（来源、版本、可执行文件、
健康检查），插件包本身仍由 npm / 官方源管理。
新增 `computer_policies`：每个公司一条三档审批策略（默认 / 自动 / 完全控制）
与自动放行白名单。硬拦截规则留在代码里，不随数据库漂移。

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plugin_installations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("install_path", sa.String(length=500), nullable=True),
        sa.Column("executable", sa.String(length=500), nullable=True),
        sa.Column("platform", sa.String(length=60), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_health_ok", sa.Boolean(), nullable=True),
        sa.Column("last_health_at", sa.DateTime(), nullable=True),
        sa.Column("last_health_error", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("installed_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "plugin_id", name="uq_plugin_company"),
    )
    op.create_index("ix_plugin_installations_company_id",
                    "plugin_installations", ["company_id"])
    op.create_index("ix_plugin_installations_plugin_id",
                    "plugin_installations", ["plugin_id"])

    op.create_table(
        "computer_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("auto_dirs", sa.JSON(), nullable=True),
        sa.Column("auto_commands", sa.JSON(), nullable=True),
        sa.Column("auto_apps", sa.JSON(), nullable=True),
        sa.Column("full_control_until", sa.DateTime(), nullable=True),
        sa.Column("notify_on_finish", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_computer_policy_company"),
    )
    op.create_index("ix_computer_policies_company_id",
                    "computer_policies", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_computer_policies_company_id",
                  table_name="computer_policies")
    op.drop_table("computer_policies")
    op.drop_index("ix_plugin_installations_plugin_id",
                  table_name="plugin_installations")
    op.drop_index("ix_plugin_installations_company_id",
                  table_name="plugin_installations")
    op.drop_table("plugin_installations")
