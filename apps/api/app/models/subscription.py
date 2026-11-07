"""公司订阅 / 试用（E2）与套餐升级订单。"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
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
    """升级 / 续费订单。

    金额刻意不下单：本仓库没有接支付渠道，也不该编造价格。订单只记录
    "哪家公司、想升到哪个套餐、谁申请的、平台何时确认收款"，收款本身
    （线下转账 / 渠道后台）在系统之外完成，确认由平台超管落章。
    """

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
