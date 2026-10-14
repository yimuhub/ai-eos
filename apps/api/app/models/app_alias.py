"""AppAliasRule: 公司级自然语言→应用 自定义映射规则。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppAliasRule(Base):
    __tablename__ = "app_alias_rules"
    __table_args__ = (
        UniqueConstraint("company_id", "alias", name="uq_alias_per_company"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(80), nullable=False)  # 口语词，如 vx/小企鹅
    app_name: Mapped[str] = mapped_column(String(120), nullable=False)  # 显示名
    app_path: Mapped[str | None] = mapped_column(String(500))  # 启动路径(.app/.lnk/.desktop)，可为空用名字启
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
