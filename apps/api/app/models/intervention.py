"""Advanced Human Intervention (P2-10).

Automation Level 0-4 per company/project/agent + approval requests +
override log.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AutomationLevel(int, enum.Enum):
    MANUAL = 0
    KEY_NODE_APPROVAL = 1
    EXCEPTION_APPROVAL = 2   # 默认
    HIGH_AUTO = 3
    FULL_AUTO = 4


class InterventionPolicy(Base):
    """自动化等级策略（公司级 / 项目级 / Agent 级）。"""
    __tablename__ = "intervention_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                    index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                  index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    approval_actions: Mapped[list | None] = mapped_column(JSON, nullable=True,
        default=lambda: ["task_create", "task_complete", "meeting_start",
                         "artifact_approve", "escalate"])
    updated_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class ApprovalRequest(Base):
    """待人工审批的请求。"""
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requested_by_agent_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="PENDING", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class OverrideLog(Base):
    """人工覆盖日志。"""
    __tablename__ = "override_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    original_decision: Mapped[dict | None] = mapped_column(JSON)
    override_decision: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    overridden_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)


def requires_approval(level: int, action_type: str,
                      approval_actions: list | None = None) -> bool:
    """判断某动作在该等级下是否需要人工审批。"""
    if level <= 0:
        return True
    if level >= 4:
        return False
    if level == 1:
        return action_type in ("task_complete", "artifact_approve", "escalate")
    if level == 3:
        return action_type == "escalate"
    actions = approval_actions if approval_actions is not None else \
        ["task_create", "task_complete", "meeting_start", "artifact_approve",
         "escalate"]
    return action_type in actions


def new_id() -> str:
    return str(uuid.uuid4())
