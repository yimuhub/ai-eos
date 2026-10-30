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
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用 [{id,name,size}]

    departments: Mapped[list["Department"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan")  # 删公司时级联删部门（company_id 为 NOT NULL，不能置 null）


class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
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
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用

    # --- P5：部门级配额与数据域（全部可空，老库靠 ensure_columns 幂等补列） ---
    agent_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 本部门 Agent 数量上限
    allowed_models: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 允许使用的 model_id 白名单
    data_domain_code: Mapped[str | None] = mapped_column(
        String(30), nullable=True, index=True)  # 数据域编码（如 finance / hr），供行级过滤预留

    company: Mapped["Company"] = relationship(back_populates="departments")
    agents: Mapped[list["Agent"]] = relationship(back_populates="department")
