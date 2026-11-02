"""老板 ↔ Agent 的私聊消息（含 CEO 常驻线程归属）。"""
from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentChatMessage(Base):
    __tablename__ = "agent_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # CEO 常驻对话的线程归属；老消息与普通员工私聊为空。
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                  index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # boss | agent
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
