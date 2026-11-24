"""公司订阅 / 试用（E2）与套餐升级订单。"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"


class SubscriptionOrderStatus(str, enum.Enum):
    """订单状态：申请 → 平台确认收款 → 已生效。取消只允许从 PENDING 进入。"""

    PENDING = "PENDING"
    PAID = "PAID"
    APPLIED = "APPLIED"
    CANCELED = "CANCELED"


class CompanySubscription(TimestampMixin, Base):
    __tablename__ = "company_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True)
    plan_code: Mapped[str] = mapped_column(
        String(40), nullable=False, default="trial")
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False, length=20), nullable=False,
        default=SubscriptionStatus.TRIALING)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seats_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SubscriptionOrder(TimestampMixin, Base):
    """升级 / 续费订单：线下转账或 Stripe 一次性付款。"""

    __tablename__ = "subscription_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    plan_code: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[SubscriptionOrderStatus] = mapped_column(
        Enum(SubscriptionOrderStatus, native_enum=False, length=20),
        nullable=False, default=SubscriptionOrderStatus.PENDING, index=True)
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="offline", server_default="offline")
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkout_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_livemode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    checkout_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class BillingWebhookEvent(Base):
    __tablename__ = "billing_webhook_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    livemode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    event_created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
