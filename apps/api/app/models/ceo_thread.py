"""CEO 常驻对话线程。

一段线程 = 老板和 CEO 的一段连续对话。`agent_chat_messages.thread_id`
指向这里；老消息 thread_id 为空，首次打开线程时归入默认线程。

`summary` 是滚动摘要：比最近几轮更早的内容先压成摘要，再进提示词，
避免每次都把整段历史塞给模型。原始消息永远保留，只有用户显式删除线程
才会真正删除——"永久保存"和"省 token"分别由存储层与摘要层承担。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class CeoThread(TimestampMixin, Base):
    __tablename__ = "ceo_threads"

    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False,
                                       default="和 CEO 的对话")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 已进入 summary 的消息条数；最近几条保持原文，保证短期上下文不失真。
    summarized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
