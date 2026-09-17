"""Agent Collaboration Protocol 2.0 models (P2-05).

12 message types for structured inter-agent communication.
Collaboration graph tracks who works with whom and in what role.
Message routing determines which agents receive each message.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CollaborationType(str, enum.Enum):
    """Agent 之间的协作关系类型。"""
    MANAGER = "manager"          # 上下级：A 管理 B
    COLLABORATOR = "collaborator"  # 平级协作：A 和 B 合作
    REPORTER = "reporter"        # 汇报：B 向 A 汇报


class AgentCollaboration(Base):
    """Agent 协作图谱边表。"""
    __tablename__ = "agent_collaborations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    source_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    target_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    collaboration_type: Mapped[CollaborationType] = mapped_column(
        Enum(CollaborationType, native_enum=False, length=50), nullable=False, default=CollaborationType.COLLABORATOR)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def __repr__(self):
        return f"AgentCollaboration({self.source_agent_id[:8]}->{self.target_agent_id[:8]})"


class CollaborationMessageType(str, enum.Enum):
    """Agent 间协作消息的 12 种类型。"""
    TASK_ASSIGNED = "TASK_ASSIGNED"    # 任务指派
    TASK_COMPLETED = "TASK_COMPLETED"  # 任务完成
    DISCUSSION = "DISCUSSION"          # 讨论发言
    VOTE = "VOTE"                      # 投票
    DECISION = "DECISION"              # 决策（达成共识）
    CONFLICT = "CONFLICT"              # 分歧/冲突
    QUESTION = "QUESTION"              # 提问
    ANSWER = "ANSWER"                  # 回答
    PROGRESS = "PROGRESS"              # 进度同步
    BLOCKED = "BLOCKED"                # 阻塞/受阻
    ESCALATE = "ESCALATE"              # 升级求助
    DONE = "DONE"                      # 任务完成确认


class AgentMessage(Base):
    """Agent 间协作消息表（P2-05 核心表）。"""
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    sender_agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    message_type: Mapped[CollaborationMessageType] = mapped_column(
        Enum(CollaborationMessageType, native_enum=False, length=50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context_ref: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                     index=True)
    context_type: Mapped[str | None] = mapped_column(String(50), nullable=True,
                                                      index=True)
    msg_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # 接收者列表（由路由系统填充）
    receiver_agent_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 是否已读
    read_by: Mapped[dict | None] = mapped_column(JSON, nullable=True,
                                                  default=dict)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)

    def __repr__(self):
        return (f"AgentMessage({self.sender_agent_id[:8]}->{self.message_type.value}:"
                f"{self.title[:40]})")
