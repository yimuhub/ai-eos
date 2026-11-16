"""公司级功能开关（V6 M4 企业定制层）。

per-company 的能力开关：行业包/插件/实验功能的启用状态。行存在 = 启用；
flag 值允许带轻量配置（JSON）。不做全局默认——基座功能按公司显式开。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, \
    UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class CompanyFeatureFlag(TimestampMixin, Base):
    __tablename__ = "company_feature_flags"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_feature_flag"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    # 惯例命名：<域>:<名>，如 industry_pack:manufacturing / toolbus:v1
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[dict | None] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                          default=True)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
