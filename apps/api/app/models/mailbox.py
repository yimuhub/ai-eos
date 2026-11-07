"""Agent 专属邮箱（V5.5 C3）。

一人公司"可被外部联系"能力：每个数字员工有自己的邮箱别名
（如 chen@aieos.local），外部来信进收件箱，Agent 可读、
老板可代答。SMTP/IMAP 通道按 Agent 配置（env 变量名）；
未配置时信件走站内收件箱（桌面 App / 局域网共享自动注入）。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    # 与全项目 naive-UTC 口径一致（修复前用本地时间，UTC+8 下快 8 小时）
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentMailbox(Base):
    """Agent 邮箱别名 + 通道配置（Secret 管理：账号只存 env 变量名）。"""
    __tablename__ = "agent_mailboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False,
                                            default="default")
    alias: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    smtp_env: Mapped[str | None] = mapped_column(String(100), nullable=True)
    imap_env: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auto_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_now)


class MailMessage(Base):
    """邮箱收/发件（统一收件箱，方向 in/out）。"""
    __tablename__ = "mail_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    mailbox_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(4), nullable=False, default="in")
    from_addr: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    to_addr: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unread")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="inbox")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_now, index=True)
