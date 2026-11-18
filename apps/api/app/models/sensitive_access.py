"""敏感访问台账（V6.2 系统日志）：谁、何时、凭什么理由、看了什么。

每次查看敏感内容（Agent 聊天记录等）写一行——查看行为本身可审计，
理由与验证方式随行留档。超级管理后台的「敏感访问」页直接展示。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SensitiveAccess(Base):
    __tablename__ = "sensitive_access"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                   index=True)
    admin_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False, index=True)
    # chat_thread | chat_group | task_trace | event_detail ...
    category: Mapped[str] = mapped_column(String(40), nullable=False,
                                          index=True)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_desc: Mapped[str | None] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # totp | recovery
    auth: Mapped[str] = mapped_column(String(20), nullable=False,
                                      default="totp")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
