"""Refresh token（可吊销）——短期 access JWT 的续期凭证。

设计要点：
- 库里只存 token 的 SHA-256 哈希：即使数据库泄漏，攻击者也拿不到
  可直接使用的 refresh token。
- `revoked` 支持登出/轮换时立即吊销；`expires_at` 是最终兜底。
- 用户删除时级联清理（FK ondelete=CASCADE）。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class RefreshToken(TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
