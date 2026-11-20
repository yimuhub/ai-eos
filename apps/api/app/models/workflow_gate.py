"""步骤级验收闸门的证据表（M1）。

一个节点每过闸门一次写一行（含返工后的复检），所以同一
`execution_id + node_key` 可能有多行：`round=0` 是首轮，`round=1` 是
返工后的复检。`attempt` 沿用 `workflow_steps.attempt`（节点被重新打开的
次数），两者语义不同，不要混用。

为什么单独建表而不是塞进 `workflow_steps.output_data`：
1. 闸门要留"哪条规则没过、实际值多少"的原始证据，评审/复盘要能复算；
2. 同一节点多轮复检要能分别查到，且不能因为返工覆盖掉首轮的失败原因；
3. 后续 AI 自主率、质量回归都要按规则维度统计，JSON 埋在输出里查不动。
"""
from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Index, Integer,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class WorkflowStepGate(TimestampMixin, Base):
    """一次闸门检查的完整结果。"""

    __tablename__ = "workflow_step_gates"
    __table_args__ = (
        Index("ix_wfgates_exec_node", "execution_id", "node_key"),
        Index("ix_wfgates_passed", "passed"),
    )

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(String(50))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    # 闸门内部的返工轮次：0 = 首检，1 = 返工后复检
    gate_round: Mapped[int] = mapped_column(Integer, default=0)

    # mechanical（纯函数断言） | judge（模型按 acceptance 评审）
    stage: Mapped[str] = mapped_column(String(20), default="mechanical")
    # [{rule, expected, actual, passed, blocking, detail}, ...]
    checks: Mapped[list] = mapped_column(JSON, default=list)

    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    # 是否存在"阻断性"失败项（passed=False 且 blocking=True）
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    # 评审模型不可用（超时/解析失败）：不算失败，但要留痕，报告里标降级
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)

    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    # 返工前的产出摘要（保留原稿证据，便于对比"改了哪一处"）
    output_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
