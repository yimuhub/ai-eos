"""Stripe checkout and durable webhook idempotency.

Revision ID: 0039
Revises: 0038
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, Sequence[str], None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("subscription_orders", sa.Column(
        "payment_method", sa.String(length=20), nullable=False,
        server_default="offline"))
    for name, type_ in (
        ("stripe_checkout_session_id", sa.String(length=255)),
        ("stripe_payment_intent_id", sa.String(length=255)),
        ("stripe_price_id", sa.String(length=255)),
        ("amount", sa.BigInteger()),
        ("currency", sa.String(length=3)),
        ("checkout_url", sa.Text()),
        ("checkout_expires_at", sa.DateTime()),
        ("receipt_url", sa.Text()),
        ("payment_livemode", sa.Boolean()),
        ("checkout_operation_id", sa.String(length=36)),
    ):
        op.add_column("subscription_orders", sa.Column(name, type_, nullable=True))
    with op.batch_alter_table("subscription_orders") as batch_op:
        batch_op.create_index(
            "ix_subscription_orders_stripe_checkout_session_id",
            ["stripe_checkout_session_id"], unique=True)
    op.create_table(
        "billing_webhook_events",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False),
        sa.Column("event_created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("billing_webhook_events")
    with op.batch_alter_table("subscription_orders") as batch_op:
        batch_op.drop_index(
            "ix_subscription_orders_stripe_checkout_session_id")
    for name in (
        "checkout_operation_id", "payment_livemode", "receipt_url",
        "checkout_expires_at", "checkout_url", "currency", "amount",
        "stripe_price_id", "stripe_payment_intent_id",
        "stripe_checkout_session_id", "payment_method",
    ):
        op.drop_column("subscription_orders", name)
