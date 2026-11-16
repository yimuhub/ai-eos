"""自然人 ↔ 数字员工绑定（V6 M5 横切 C：人机组织模型）。

每个自然人可以拥有多个 Agent（随身助理/下属/顾问），一个老板可以
拥有一个军团。绑定是「谁在用这个员工」的归属关系：
* assistant   —— 随身助理：跨任务跟随该自然人（1 人 1 个的语义在前端约束）
* subordinate —— 下属：该自然人指挥它干活
* consultant  —— 顾问：按需咨询，不承担例行工作
"""
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin

RELATIONS = ("assistant", "subordinate", "consultant")


class MemberAgentBinding(TimestampMixin, Base):
    __tablename__ = "member_agent_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_id", name="uq_member_agent"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    relation: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="subordinate")
    created_by: Mapped[str | None] = mapped_column(String(36))
