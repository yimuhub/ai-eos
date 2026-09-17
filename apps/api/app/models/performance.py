"""Agent Performance System (P2-11).

真实指标统计：从 agent_executions / tasks / meeting_participants /
agent_messages 聚合每个 Agent 的绩效，无人工填报。
指标：
- 任务总数 / 完成数 / 完成率
- 平均执行时长（秒）
- 失败次数
- 会议发言次数
- 协作消息发送量
- 综合绩效分（0-100）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PerformanceSnapshot(Base):
    """绩效快照（定期落库，便于看趋势）。"""
    __tablename__ = "performance_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(nullable=False)
    period_end: Mapped[datetime] = mapped_column(nullable=False)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_now)
