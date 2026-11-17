"""商业交付三引擎（V6.1）：合规台账 / 工资规则 / 对账

新增三张表：compliance_ledgers（证照台账+到期预警）、payroll_rules
（工资/分佣规则）、reconciliation_runs（规整层双源对账）。
对应模型 `app/models/ops.py`。

Revision ID: 0024
Revises: 0023
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, Sequence[str], None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts():
    return (
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "compliance_ledgers",
        *_ts(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("authority", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="manual"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("renewal_rule", sa.Text(), nullable=True),
        sa.Column("renewal_days_before", sa.Integer(), nullable=False,
                  server_default="30"),
        sa.Column("penalty", sa.Text(), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("owner_agent_id", sa.String(length=36), nullable=True),
        sa.Column("pack_id", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_compliance_key"),
    )
    op.create_index("ix_compliance_ledgers_company_id", "compliance_ledgers",
                    ["company_id"])
    op.create_index("ix_compliance_ledgers_key", "compliance_ledgers", ["key"])
    op.create_index("ix_compliance_ledgers_status", "compliance_ledgers",
                    ["status"])
    op.create_index("ix_compliance_ledgers_pack_id", "compliance_ledgers",
                    ["pack_id"])

    op.create_table(
        "payroll_rules",
        *_ts(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False,
                  server_default="piece"),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_payroll_key"),
    )
    op.create_index("ix_payroll_rules_company_id", "payroll_rules",
                    ["company_id"])

    op.create_table(
        "reconciliation_runs",
        *_ts(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="running"),
        sa.Column("total_a", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_b", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("only_a", sa.JSON(), nullable=True),
        sa.Column("only_b", sa.JSON(), nullable=True),
        sa.Column("mismatched", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reconciliation_runs_company_id", "reconciliation_runs",
                    ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_reconciliation_runs_company_id",
                  table_name="reconciliation_runs")
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_payroll_rules_company_id", table_name="payroll_rules")
    op.drop_table("payroll_rules")
    op.drop_index("ix_compliance_ledgers_pack_id", table_name="compliance_ledgers")
    op.drop_index("ix_compliance_ledgers_status", table_name="compliance_ledgers")
    op.drop_index("ix_compliance_ledgers_key", table_name="compliance_ledgers")
    op.drop_index("ix_compliance_ledgers_company_id",
                  table_name="compliance_ledgers")
    op.drop_table("compliance_ledgers")
