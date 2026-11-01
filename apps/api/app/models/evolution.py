"""CEO 自我进化：运行人格（playbook）与分级进化记录。

## 为什么要有 playbook

「让 CEO 拟人化地成长」如果只写进一句聊天记录，第二天就找不到了。
真正可积累的东西是**被版本化的运行人格**：风险姿态、决策原则、参数偏好。
每次日/周/月进化产出新版本，旧版本保留——老板能回看「第几版开始把失败率
降下来的」，也能在升坏了的时候对照上一版。

## 为什么分日 / 周 / 月三档

* **日**：只微调原则（1–3 条），不改人格、不动参数。便宜、高频，
  让 CEO 每天都能从昨天的事里学一点，但不会因为一天的噪声就换性格。
* **周**：结构性复盘——结合周考与一周运行数据，重写原则、调整人格与参数。
* **月**：迭代级——只产出**提案**（组织、工作流、成本），高风险动作必须
  由老板拍板；低风险动作可随月度记录直接生效。自动化优先，但不可逆的事
  不替老板做主。

## 幂等

同一家公司、同一档位、同一个周期（日=日期，周=ISO 周，月=年月）只允许
一条记录。UniqueConstraint 是最后一道闸：调度租约过期、手动补跑、多端
同时点击都不会把同一段运行数据进化两遍、多花一次模型钱。
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class EvolutionLevel(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class EvolutionStatus(str, Enum):
    APPLIED = "APPLIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class CeoPlaybook(TimestampMixin, Base):
    """CEO 的版本化运行人格。同公司同时只有一条 active。"""
    __tablename__ = "ceo_playbooks"
    __table_args__ = (
        Index("ix_ceo_playbooks_company_active", "company_id", "active"),
        UniqueConstraint("company_id", "version", name="uq_ceo_playbook_version"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # seed | daily | weekly | monthly | exam | manual
    source: Mapped[str] = mapped_column(String(20), default="seed")
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # {"risk_posture": "稳健型", "decision_style": "先算成本再动手", "traits": [...]}
    persona: Mapped[dict] = mapped_column(JSON, default=dict)
    principles: Mapped[list] = mapped_column(JSON, default=list)
    # 可被代码直接执行的偏好：质量线、成本上限倍数、并发上限、补跑预算
    adjustments: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    lessons: Mapped[list] = mapped_column(JSON, default=list)
    # 本版来自哪条进化记录（便于从 playbook 回溯运行数据）
    evolution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class EvolutionRecord(TimestampMixin, Base):
    """一次日/周/月进化：输入(指标) → 诊断 → 动作 → 结果。"""
    __tablename__ = "evolution_records"
    __table_args__ = (
        UniqueConstraint("company_id", "level", "period_key",
                         name="uq_evolution_period"),
        Index("ix_evolution_company_created", "company_id", "created_at"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(20), default="daily", index=True)
    # 2026-11-01 / 2026-W44 / 2026-11
    period_key: Mapped[str] = mapped_column(String(20))
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=EvolutionStatus.APPLIED.value, index=True)

    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {"score": 72.5, "headline": "...", "issues": [...], "wins": [...]}
    assessment: Mapped[dict] = mapped_column(JSON, default=dict)
    # [{id,type,title,detail,risk,applied,approved,decided_by,decided_at,note}]
    actions: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    playbook_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    playbook_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exam_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本次进化花了多少 token（成本必须可审计，否则「自我进化」会变成烧钱黑洞）
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(20), default="schedule")
