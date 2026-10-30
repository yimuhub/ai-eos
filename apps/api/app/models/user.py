"""User accounts (P1-02)."""
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 安装期建立的平台超管：可进入任意公司（公司内就地判定为 ADMIN）。
    # 可空是为了老库能靠 ensure_columns 自动补列，历史用户升级后为 NULL（= 非超管）。
    is_superadmin: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False, index=True)
