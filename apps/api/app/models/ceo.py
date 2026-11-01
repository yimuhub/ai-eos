"""超级 Agent（CEO）：一次统筹的准绳与执行台账。

## 为什么只有一张表

「准绳」和「这一次统筹的进度」是同一个生命周期：锚在开工前定、之后只增不改；
执行台账（子任务数、完成数、偏离数、报告）只对这一次统筹有意义。
拆成两张表会让「锚版本 → 哪次执行用了哪版」多出一层 join，而老板要看的
始终是「这一次任务现在到哪了、锚是什么」，单表更贴近使用方式。

## 锚不可变

`PLANNING` 阶段允许改锚（并且 version +1）；`RUNNING` 之后只允许
`extra_instructions` 追加（老板中途补一句要求），不能改目标/红线。
强制点写在路由里（409），不是靠调用方自觉——否则「准绳」就是一句口号。

## 状态

PLANNING → RUNNING → RECLAIMING（回收补跑/审查）→ COMPLETED / FAILED
RUNNING 期间可 PAUSED（暂停放行新子任务）→ RUNNING；
任意未终结状态可 CANCELLED。
"""
from sqlalchemy import (JSON, Boolean, ForeignKey, Index, Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class CeoMission(TimestampMixin, Base):
    __tablename__ = "ceo_missions"
    __table_args__ = (
        Index("ix_ceo_missions_company_status", "company_id", "status"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    coordinator_agent_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))

    title: Mapped[str] = mapped_column(String(300))
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="PLANNING", index=True)

    # --- 准绳 ---
    objective: Mapped[str | None] = mapped_column(Text)
    deliverable: Mapped[str | None] = mapped_column(Text)
    redlines: Mapped[list] = mapped_column(JSON, default=list)
    acceptance: Mapped[list] = mapped_column(JSON, default=list)
    terminology: Mapped[dict] = mapped_column(JSON, default=dict)
    extra_instructions: Mapped[list] = mapped_column(JSON, default=list)
    anchor_version: Mapped[int] = mapped_column(Integer, default=1)

    # --- 并发闸门 ---
    max_parallel: Mapped[int] = mapped_column(Integer, default=3)
    effective_parallel: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    degraded: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    degrade_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded_until: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- 执行台账 ---
    plan: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 本次统筹自动新招的员工：开工前是提议（无 agent_id），开工后回填真实 id。
    # 单独一列而不是塞进 stats：这是老板要审计的「组织变化」，不是统计数字。
    new_agents: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    subtask_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    off_track_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 本次统筹生成/复用的工作流（开工时落库，收尾后可能升到新版本）
    workflow_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
