"""Advanced Automation Engine (P3-12).

多步自动化：steps 顺序执行，支持条件/审批/重试/子流程。
防失控：深度限制、速率限制、循环检测、Dry Run 模式。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


MAX_DEPTH = 3          # 子流程嵌套上限
MAX_STEPS_PER_RUN = 50 # 单次运行步数上限


class AutomationDefinition(Base):
    __tablename__ = "automation_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False,
                                               default="MANUAL")
    # TIME / EVENT / FILE / PROJECT / TASK / AGENT / WEBHOOK / HUMAN
    trigger_config: Mapped[dict | None] = mapped_column(JSON)
    steps: Mapped[list | None] = mapped_column(JSON, default=list)
    # [{seq,type,params,condition?,on_failure?}] type: send_message/create_task/
    # start_workflow/start_meeting/call_skill/run_sub_automation/wait_approval
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="TEST")
    # TEST(DryRun) / SIMULATION / PRODUCTION
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="DRAFT", index=True)
    max_runs_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用


class AutomationRunRecord(Base):
    """一次自动化运行（含每步结果）。"""
    __tablename__ = "automation_run_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("automation_definitions.id", ondelete="CASCADE"),
        nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)   # 执行时模式
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="RUNNING", index=True)
    step_results: Mapped[list | None] = mapped_column(JSON)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


def new_id():
    return str(uuid.uuid4())
