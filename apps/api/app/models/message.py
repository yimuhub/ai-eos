"""Agent communication system (P1-09)."""
import enum

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class MessageType(str, enum.Enum):
    MESSAGE = "MESSAGE"
    TASK_REQUEST = "TASK_REQUEST"
    TASK_RESPONSE = "TASK_RESPONSE"
    HELP_REQUEST = "HELP_REQUEST"
    RESULT = "RESULT"
    SYSTEM = "SYSTEM"
    USER = "USER"


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    participants: Mapped[list | None] = mapped_column(JSON, default=list)  # [agent ids]


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender_type: Mapped[str] = mapped_column(String(10))  # agent|user|system
    sender_id: Mapped[str | None] = mapped_column(String(36))
    receiver_id: Mapped[str | None] = mapped_column(String(36))
    message_type: Mapped[MessageType] = mapped_column(Enum(MessageType, native_enum=False, length=50), default=MessageType.MESSAGE)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
