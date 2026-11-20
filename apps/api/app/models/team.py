"""角色档案与阵容锁定（M3-B）。

`RoleProfile` = 能力条目（"财务分析师"该有什么验收标准），可以跨工作流复用；
`TeamLoadout` = 一次跑得好的阵容（存**角色 key**，不存员工 id），
导入 spec 时用它锁定可选角色，防漏人、防幻觉岗位。
"""
from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class RoleProfile(TimestampMixin, Base):
    __tablename__ = "role_profiles"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)   # 公司内唯一
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(100))
    capability_tags: Mapped[list | None] = mapped_column(JSON, default=list)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    # 这个角色的"出厂验收标准"：工作流不写 gate 时可继承（本轮只存，不自动套用）
    default_gate: Mapped[dict | None] = mapped_column(JSON, default=dict)
    model_tier: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="custom")


class TeamLoadout(TimestampMixin, Base):
    __tablename__ = "team_loadouts"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # [{"key": "finance-analyst", "name": "财务分析师"}]
    roles: Mapped[list | None] = mapped_column(JSON, default=list)
    default_inputs: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text)
