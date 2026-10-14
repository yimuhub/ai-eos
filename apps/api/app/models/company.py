"""Company & Department organization (P1-03)."""
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from .base import TimestampMixin

if TYPE_CHECKING:
    from .agent import Agent


class Company(TimestampMixin, Base):
    __tablename__ = "companies"

    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    logo: Mapped[str | None] = mapped_column(String(500))

    departments: Mapped[list["Department"]] = relationship(back_populates="company")


class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    parent_department_id: Mapped[str | None] = mapped_column(
        ForeignKey("departments.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    manager_agent_id: Mapped[str | None] = mapped_column(String(36))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)      # 卡片排序（小在前）
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)     # 置顶
    skill_ids: Mapped[list] = mapped_column(JSON, default=list)      # 部门专属技能
    kb_document_ids: Mapped[list] = mapped_column(JSON, default=list)  # 部门专属知识库文档

    company: Mapped["Company"] = relationship(back_populates="departments")
    agents: Mapped[list["Agent"]] = relationship(back_populates="department")
