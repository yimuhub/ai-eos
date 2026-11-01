"""公司订阅 / 试用（E2）。"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
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
