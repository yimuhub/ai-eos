"""技能库条目（V6.2 专家技能引入方案）。

外部技能生态（WorkBuddy/CodeBuddy 公开市场的 SKILL.md / mcp.json /
专家团队包）的统一收录、审核与发行台账。企业端只见 published 的平台条目。
"""
from sqlalchemy import JSON, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin

# kind 与 review_status 用纯 String 列（路由层校验取值），
# 避免 SQLAlchemy Enum 的 name/value 混淆——这张表是收录台账，不是运行态。
KINDS = ("skill", "connector", "expert")
STATUSES = ("auto_pass", "review", "published", "rejected")


class SkillLibraryItem(TimestampMixin, Base):
    """技能库条目：source+source_id 唯一（同一外部技能只收录一次）。"""
    __tablename__ = "skill_library"
    __table_args__ = (UniqueConstraint("source", "source_id",
                                       name="uq_skill_library_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(30), default="workbuddy",
                                        index=True)  # workbuddy|builtin|社区投递
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="skill", index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    description: Mapped[str | None] = mapped_column(Text)
    # 原始内容整体留存：skill={"frontmatter","body"}；connector={"mcp"}；
    # expert={"agents":[{name,title,md}],"readme","settings"}
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    trigger_keywords: Mapped[list | None] = mapped_column(JSON, default=list)
    industry_tags: Mapped[list | None] = mapped_column(JSON, default=list)
    license: Mapped[str | None] = mapped_column(String(60), nullable=True)
    brand_refs: Mapped[list | None] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default="review",
                                               index=True)
    reviewer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # NULL=平台库；预留企业自建/投递通道
    company_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                   index=True)
    installed_companies: Mapped[list | None] = mapped_column(JSON, default=list)
    install_count: Mapped[int] = mapped_column(Integer, default=0)
