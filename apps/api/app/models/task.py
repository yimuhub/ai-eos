"""Task engine + runtime execution state (P1-07/08/14/15)."""
import enum

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class TaskEventType(str, enum.Enum):
    CREATED = "CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    ASSIGNED = "ASSIGNED"
    INTERVENTION = "INTERVENTION"
    ERROR = "ERROR"


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_company_status", "company_id", "status"),)

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, native_enum=False, length=50), default=TaskStatus.TODO, index=True)
    priority: Mapped[TaskPriority] = mapped_column(Enum(TaskPriority, native_enum=False, length=50), default=TaskPriority.NORMAL)
    assigned_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    result: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(String(50))
    completed_at: Mapped[str | None] = mapped_column(String(50))


class TaskEvent(TimestampMixin, Base):
    """Immutable state-change log per task."""
    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    event_type: Mapped[TaskEventType] = mapped_column(Enum(TaskEventType, native_enum=False, length=50))
    actor_type: Mapped[str] = mapped_column(String(20))  # user|agent|system
    actor_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)


class AgentExecution(TimestampMixin, Base):
    """One run of an agent against a task (P1-08)."""
    __tablename__ = "agent_executions"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running|completed|failed|paused|cancelled
    input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 完成时间。原先 agent_runtime 与 tasks 路由都会写 execution.completed_at，
    # 但模型上没有这一列——赋值只是挂了个临时 Python 属性，**入库时静默丢弃**，
    # 于是"单次执行耗时"永远查不出来。端到端试跑核对表结构时发现的。
    completed_at: Mapped[str | None] = mapped_column(String(50))


class ExecutionCheckpoint(TimestampMixin, Base):
    """Runtime checkpoints enabling true Pause/Resume (P1-14)."""
    __tablename__ = "execution_checkpoints"

    execution_id: Mapped[str] = mapped_column(ForeignKey("agent_executions.id"), index=True)
    checkpoint_type: Mapped[str] = mapped_column(String(30))  # planning|execution|result
    step_index: Mapped[int] = mapped_column(default=0)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON, default=dict)


class Intervention(TimestampMixin, Base):
    """Human-in-the-loop inputs into running tasks (P1-15)."""
    __tablename__ = "interventions"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(30))  # MESSAGE|INSTRUCTION|FILE|URL|PRIORITY_CHANGE|AGENT_CHANGE
    content: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|delivered|applied

