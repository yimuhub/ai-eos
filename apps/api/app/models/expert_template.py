"""Expert Template Library (V3.0 专家库融合).

从 agency-agents-zh 仓库导入的专家人设模板。
模板 = 工种（可重复雇佣），上岗走工厂确认链（蓝图审查→验收→上岗）。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ExpertTemplate(Base):
    __tablename__ = "expert_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 来源
    source: Mapped[str] = mapped_column(String(50), default="agency-agents-zh")
    source_url: Mapped[str | None] = mapped_column(String(300))
    license: Mapped[str | None] = mapped_column(String(50), default="MIT")
    source_slug: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)  # 如 engineering-ai-engineer
    version: Mapped[str] = mapped_column(String(20), default="1.0")
    # 人设
    name: Mapped[str] = mapped_column(String(200))                # frontmatter name
    emoji: Mapped[str | None] = mapped_column(String(10))
    color: Mapped[str | None] = mapped_column(String(20))
    department: Mapped[str] = mapped_column(String(100), default="general")  # 工种部门目录
    tags: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    # 人设正文（身份/使命/规则/交付物/流程）
    system_prompt: Mapped[str] = mapped_column(Text, default="")   # 截断至 ~6KB 核心
    raw_md: Mapped[str | None] = mapped_column(Text)               # 完整原文（详情页查看）
    # 交付物清单（从正文 "交付物" 章节提取，供验收出题用）
    deliverables: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hired_count: Mapped[int] = mapped_column(Integer, default=0)   # 被雇佣次数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def public_dict(self):
        return {
            "id": self.id, "source": self.source, "source_url": self.source_url,
            "source_slug": self.source_slug, "name": self.name,
            "emoji": self.emoji, "color": self.color, "license": self.license,
            "department": self.department, "tags": self.tags or [],
            "description": (self.description or "")[:300],
            "deliverables": (self.deliverables or [])[:8],
            "enabled": self.enabled, "hired_count": self.hired_count,
            "prompt_chars": len(self.system_prompt or ""),
            "has_raw_md": bool(self.raw_md),
        }
