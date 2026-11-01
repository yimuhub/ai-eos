"""可共享的 Agent 经验与已验证工作流。

一次任务重试多次、既有失败又有成功时，真正值钱的不是「最后做成了」，
而是「前两次为什么失败、第三次改了什么」。这些经验以前只留在执行记录里，
下一个 Agent 接同类任务时仍会重蹈覆辙。

本表把经验变成公司资产：
* `success`：成功路径（步骤、成本、质量要点），可被直接复现；
* `failure`：失败教训（踩坑、错误信号、止损线），在同类任务开工前提醒；
* `scope=company`：默认全员可见，实现跨 Agent 学习；`private` 只给作者本人。

经验按 `task_id + agent_id` 幂等：同一任务的成功/失败反复上报只更新一条，
不会因为调用方多挂一次钩子就刷出一堆重复资产。
"""
from __future__ import annotations

from sqlalchemy import (JSON, Float, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin


class SharedExperience(TimestampMixin, Base):
    __tablename__ = "shared_experiences"

    company_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    author_agent_name: Mapped[str | None] = mapped_column(String(200),
                                                          nullable=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    task_title: Mapped[str] = mapped_column(String(500))
    # 同名任务共享一个复用键：下一位 Agent 靠它找到这条经验和工作流
    reuse_key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="success", index=True)
    scope: Mapped[str] = mapped_column(String(20), default="company", index=True)
    status: Mapped[str] = mapped_column(String(20), default="published",
                                        index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pitfalls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    lessons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    token_tips: Mapped[list | None] = mapped_column(JSON, nullable=True)
    quality_tips: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    tokens_saved_est: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                    index=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adopted_by: Mapped[list | None] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20), default="runtime")
    meta: Mapped[dict | None] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("task_id", "agent_id",
                         name="uq_shared_experiences_task_agent"),
        Index("ix_shared_experiences_company_key", "company_id", "reuse_key"),
    )
