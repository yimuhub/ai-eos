"""Advanced Workflow Engine models (P2-02)."""
import enum

from sqlalchemy import (JSON, Boolean, Enum, Float, ForeignKey, Index, Integer,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class WorkflowStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class NodeType(str, enum.Enum):
    START = "START"
    END = "END"
    TASK = "TASK"                # 普通任务节点
    AGENT = "AGENT"              # Agent 执行节点
    CONDITION = "CONDITION"      # 条件分支
    PARALLEL = "PARALLEL"        # 并行开始
    MERGE = "MERGE"              # 并行汇聚
    WAIT = "WAIT"                # 等待（秒）
    APPROVAL = "APPROVAL"        # 人工审批
    HUMAN_INPUT = "HUMAN_INPUT"  # 人工输入
    SUB_WORKFLOW = "SUB_WORKFLOW"


class RunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INPUT = "WAITING_INPUT"


class WorkflowDefinition(TimestampMixin, Base):
    __tablename__ = "workflow_definitions"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(WorkflowStatus, native_enum=False, length=50), default=WorkflowStatus.DRAFT)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))

    # --- CEO 生成与自优化 ---
    # 同一个 reuse_key 下每次生成/优化是一个新版本；旧版本归档而不是删除，
    # 便于对比「第几版把失败率降下来了」。
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    owner_agent_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    reuse_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True)
    previous_workflow_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    # 生成依据（知识库命中 / 网络来源 / 拆解签名 / 累计经验），全部可审计
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)


class WorkflowNode(TimestampMixin, Base):
    __tablename__ = "workflow_nodes"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_definitions.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(String(50))   # 图内唯一标识 n1/n2...
    node_type: Mapped[NodeType] = mapped_column(Enum(NodeType, native_enum=False, length=50))
    name: Mapped[str] = mapped_column(String(300))
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # AGENT: {agent_id, task_title_template, instruction}
    # CONDITION: {expression: "output.contains('完整')"} 简化版：{keyword}
    # PARALLEL/MERGE: 分支数 branch_count
    # WAIT: {seconds} / APPROVAL: {approver_hint} / SUB_WORKFLOW: {workflow_id}
    position_x: Mapped[float] = mapped_column(Float, default=0)   # 可视化编辑器坐标
    position_y: Mapped[float] = mapped_column(Float, default=0)


class WorkflowEdge(TimestampMixin, Base):
    __tablename__ = "workflow_edges"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_definitions.id", ondelete="CASCADE"), index=True)
    from_node: Mapped[str] = mapped_column(String(50))
    to_node: Mapped[str] = mapped_column(String(50))
    label: Mapped[str | None] = mapped_column(String(100))  # 条件分支标签：是/否


class WorkflowExecution(TimestampMixin, Base):
    """一次工作流运行实例 —— 全部状态持久化在后端。"""
    __tablename__ = "workflow_executions"

    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_definitions.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False, length=50), default=RunStatus.PENDING, index=True)
    current_nodes: Mapped[list | None] = mapped_column(JSON, default=list)  # 当前活跃 node_key 列表
    input_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
    output_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class WorkflowFeedback(TimestampMixin, Base):
    """一次运行后的评分与经验：优化下一版工作流的唯一输入。

    评分刻意做成确定性公式（见 workflow_authoring.score_mission），不用模型：
    同一批结果永远得到同一个分数，老板能自己复算，复盘时不会各说各话。
    """
    __tablename__ = "workflow_feedback"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_definitions.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="ceo")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(Float, default=0)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    lessons: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class WorkflowStep(TimestampMixin, Base):
    """每个节点的执行记录。"""
    __tablename__ = "workflow_steps"
    __table_args__ = (Index("ix_wfsteps_exec_node", "execution_id", "node_key"),)

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(String(50))
    node_type: Mapped[NodeType] = mapped_column(Enum(NodeType, native_enum=False, length=50))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False, length=50), default=RunStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    input_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
    output_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(String(50))
    completed_at: Mapped[str | None] = mapped_column(String(50))
