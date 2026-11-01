"""BYOK 密钥保险库（E8）：只存密文，明文永不落库。"""
import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class CompanySecret(TimestampMixin, Base):
    __tablename__ = "company_secrets"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_company_secret_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
