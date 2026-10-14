"""Agent Memory System (P2-07).

5 memory types with importance filtering and TTL expiry:
- EPISODIC: 事件记忆（做了什么事）
- SEMANTIC: 事实/知识记忆（学到了什么）
- PROCEDURAL: 流程记忆（怎么做某类事）
- WORKING: 工作记忆（当前任务上下文，短TTL）
- REFLECTION: 反思记忆（总结教训）
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MemoryType(str, enum.Enum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"
    WORKING = "WORKING"
    REFLECTION = "REFLECTION"


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, native_enum=False, length=50), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    context_ref: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                     index=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)


# 各类型默认 TTL（秒）；None=永久
DEFAULT_TTL = {
    MemoryType.EPISODIC: 30 * 24 * 3600,     # 30 天
    MemoryType.SEMANTIC: None,                # 永久
    MemoryType.PROCEDURAL: None,              # 永久
    MemoryType.WORKING: 2 * 3600,             # 2 小时
    MemoryType.REFLECTION: 90 * 24 * 3600,   # 90 天
}

# 各类型默认重要度阈值（低于此值的在容量压力时先被清理）
DEFAULT_IMPORTANCE = {
    MemoryType.EPISODIC: 0.3,
    MemoryType.SEMANTIC: 0.5,
    MemoryType.PROCEDURAL: 0.6,
    MemoryType.WORKING: 0.2,
    MemoryType.REFLECTION: 0.4,
}
