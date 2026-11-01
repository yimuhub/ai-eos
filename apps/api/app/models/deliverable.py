"""交付评论（B2）：成果维度的讨论记录。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DeliverableComment(Base):
    __tablename__ = "deliverable_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False,
        index=True)
    author_type: Mapped[str] = mapped_column(String(20), nullable=False)  # agent|user|system
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)
