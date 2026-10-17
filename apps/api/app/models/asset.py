"""Asset center (P1-12)."""
import enum

from sqlalchemy import BigInteger, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class AssetType(str, enum.Enum):
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"
    CODE = "CODE"
    DATA = "DATA"
    REPORT = "REPORT"
    OTHER = "OTHER"


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    type: Mapped[AssetType] = mapped_column(Enum(AssetType, native_enum=False, length=50), default=AssetType.OTHER)
    path: Mapped[str] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(default=1)
    created_by: Mapped[str | None] = mapped_column(String(36))  # user or agent id
    creator_type: Mapped[str | None] = mapped_column(String(10))  # user|agent
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
