"""套餐升级/续费订单

新增 `subscription_orders`：公司申请升级套餐 → 平台确认收款（PAID）→
应用订单（APPLIED）。金额刻意不下单（没有接支付渠道，不编造价格）；
`confirmed_by` 记录谁盖的章，可对账。

Revision ID: 0016
Revises: 0015
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscription_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("plan_code", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("requested_by", sa.String(length=36), nullable=True),
        sa.Column("confirmed_by", sa.String(length=36), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscription_orders_company_id",
                    "subscription_orders", ["company_id"])
    op.create_index("ix_subscription_orders_status",
                    "subscription_orders", ["status"])


def downgrade() -> None:
    op.drop_index("ix_subscription_orders_status",
                  table_name="subscription_orders")
    op.drop_index("ix_subscription_orders_company_id",
                  table_name="subscription_orders")
    op.drop_table("subscription_orders")
