"""自动组织架构生成：一次分析 = 一条可追溯的 run。

老板把公司资料放进知识库后，系统读资料 → 形成公司画像 → 设计组织蓝图
→ 交叉审查 → 人工审阅 → 一键落地。每一步的输入快照、阶段耗时、
草稿与最终建出的部门/员工 id 都落在这张表里，便于复现、回滚和审计。
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class OrgSynthesisStatus(str, enum.Enum):
    ANALYZING = "ANALYZING"
    READY = "READY"              # 草稿已出，可审阅/调整/落地
    APPLIED = "APPLIED"          # 已落地到部门与员工
    ROLLED_BACK = "ROLLED_BACK"  # 按快照撤销
    FAILED = "FAILED"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OrgSynthesisRun(TimestampMixin, Base):
    __tablename__ = "org_synthesis_runs"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[OrgSynthesisStatus] = mapped_column(
        Enum(OrgSynthesisStatus, native_enum=False, length=20),
        nullable=False, default=OrgSynthesisStatus.ANALYZING, index=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False,
                                       default="inventory")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 输入快照：文档清单、现有组织、老板补充说明。资料后来被删也能复盘。
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 四段分析：每段的名称、耗时、产出摘要
    stages: Mapped[list | None] = mapped_column(JSON, default=list)
    # 审阅草稿：profile / centers / departments / agents / warnings / totals
    draft: Mapped[dict | None] = mapped_column(JSON, default=dict)
    usage: Mapped[dict | None] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 落地快照：本次新建的 center/department/agent id，回滚只动这些行。
    snapshot: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
