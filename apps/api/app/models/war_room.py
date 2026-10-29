"""项目作战室（War Room）—— 老板下达目标 → 负责人分析拆解 → 组队 →
群聊宣告互认 → 并行执行 → 产出物汇总 → 可视化审核。

核心表：
- war_room_sessions   一次作战会话（对应一个项目的启动/阶段）
- war_room_tasks      拆解后的分工任务（含宣告/确认状态、产出物、审核链）
"""
from sqlalchemy import Boolean, Column, String, Text, JSON, DateTime, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class _TS:
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now())
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())


class WarRoomSession(Base, _TS):
    __tablename__ = "war_room_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), index=True)
    goal: Mapped[str] = mapped_column(Text)                  # 老板原话目标
    leader_agent_id: Mapped[str] = mapped_column(String(36))  # 项目负责人（总指挥）
    status: Mapped[str] = mapped_column(String(20), default="analyzing", index=True)
    # analyzing -> announcing(成员宣告中) -> confirmed(负责人校对通过) ->
    # executing(并行执行) -> delivering(汇总交付) -> review(审核) -> done / failed
    analysis: Mapped[dict | None] = mapped_column(JSON)       # 负责人的整体分析
    deliverable_format: Mapped[str | None] = mapped_column(String(30))  # 统一交付格式 word/md/ppt...
    deliverable_rule: Mapped[str | None] = mapped_column(Text)          # 统一交付规则描述
    final_output_path: Mapped[str | None] = mapped_column(String(500))  # 最终整合产出物
    # 一句话直达：老板已用一句指令授权，跳过逐人校对与逐条审核，
    # 全部成员交付后自动整合出最终文件（不再需要人点"整合交付"）。
    auto_run: Mapped[bool | None] = mapped_column(Boolean, default=False)


class WarRoomTask(Base, _TS):
    __tablename__ = "war_room_tasks"
    __table_args__ = (Index("ix_wrt_session", "session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(default=0)              # 任务序号 1..N
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str | None] = mapped_column(Text)
    # 宣告与确认协议
    announce_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending -> announced(Agent已在群聊复述) -> acknowledged(负责人确认正确) / corrected(负责人提出纠正)
    announce_text: Mapped[str | None] = mapped_column(Text)   # Agent 在群里的复述原文
    leader_feedback: Mapped[str | None] = mapped_column(Text) # 负责人确认/纠正原文
    status: Mapped[str] = mapped_column(String(20), default="todo", index=True)
    # todo -> running -> submitted(已交产出物) -> approved / rejected
    deliverable_path: Mapped[str | None] = mapped_column(String(500))   # 产出物文件路径
    deliverable_name: Mapped[str | None] = mapped_column(String(300))
    deliverable_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(40))  # leader / boss
    review_comment: Mapped[str | None] = mapped_column(Text)
