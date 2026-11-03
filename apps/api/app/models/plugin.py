"""插件登记与本机操作策略。

`plugin_installations` 记录「这台机器上装了哪个插件」，不是包管理器：
包本身由 npm / 官方源管理，这里只负责识别、登记、健康检查与审计。
`computer_policies` 每个公司一条，保存三档审批模式与自动放行白名单；
硬拦截规则不落库，始终由服务端代码决定，避免被前端改写绕过。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class PluginInstallation(TimestampMixin, Base):
    __tablename__ = "plugin_installations"
    __table_args__ = (
        UniqueConstraint("company_id", "plugin_id", name="uq_plugin_company"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    plugin_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[str | None] = mapped_column(String(40))
    # installed / running / disabled / error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="installed")
    # managed = 本系统执行安装；cli = 用户自行安装后检测登记
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="managed")
    install_path: Mapped[str | None] = mapped_column(String(500))
    executable: Mapped[str | None] = mapped_column(String(500))
    platform: Mapped[str | None] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_health_error: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    installed_by: Mapped[str | None] = mapped_column(String(36))


class ComputerPolicy(TimestampMixin, Base):
    __tablename__ = "computer_policies"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_computer_policy_company"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    # default = 逐次审批；auto = 白名单放行；full = 完全控制（限时）
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="default")
    auto_dirs: Mapped[list | None] = mapped_column(JSON, default=list)
    auto_commands: Mapped[list | None] = mapped_column(JSON, default=list)
    auto_apps: Mapped[list | None] = mapped_column(JSON, default=list)
    full_control_until: Mapped[datetime | None] = mapped_column(DateTime)
    notify_on_finish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(36))
