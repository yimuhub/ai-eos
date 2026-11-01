"""Automation Scheduler (P2-15).

定时/事件触发的持久化调度：
- schedule: cron 表达式或 interval 秒
- 触发动作：启动工作流 / 发协作消息 / 创建任务
- 每次触发记录 run 历史；错过的执行按 next_run_at 补跑一次（幂等）
- 调度循环在 API 进程内 asyncio task 运行，状态全部持久化（重启恢复）
"""
import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ScheduleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 触发器：interval 秒数 或 cron 简化版（"daily@09:00"）
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # interval: INTERVAL_SECONDS / daily@HH:MM
    trigger_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 动作：start_workflow / send_message / create_task / snapshot_performance
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    action_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[ScheduleStatus] = mapped_column(
        String(20), nullable=False, default="ACTIVE", index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=True,
                                                   index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用


class AutomationRun(Base):
    """每次触发的运行记录。"""
    __tablename__ = "automation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_schedules.id", ondelete="CASCADE"),
        nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="RUNNING")  # RUNNING/SUCCESS/FAILED
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)


def compute_next_run(trigger_type: str, config: dict | None,
                     after: datetime) -> datetime:
    """计算下次运行时间。

    支持 interval / daily / weekly / monthly 四种触发器。
    weekly 的 weekday 用 ISO 约定（周一=0）；monthly 的 day 夹在 1–28，
    避免「31 号」在 2 月永远不触发、也让月末行为可预测。
    """
    if trigger_type == "interval":
        seconds = int((config or {}).get("seconds", 60))
        return after + timedelta(seconds=seconds)
    if trigger_type == "daily":
        hh, mm = ((config or {}).get("time", "09:00")).split(":")
        candidate = after.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if trigger_type == "weekly":
        hh, mm = ((config or {}).get("time", "09:00")).split(":")
        weekday = int((config or {}).get("weekday", 0)) % 7
        candidate = after.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate
    if trigger_type == "monthly":
        hh, mm = ((config or {}).get("time", "09:00")).split(":")
        day = min(max(int((config or {}).get("day", 1)), 1), 28)
        candidate = after.replace(day=day, hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
        if candidate <= after:
            year = after.year + (1 if after.month == 12 else 0)
            month = 1 if after.month == 12 else after.month + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate
    # 默认 5 分钟后
    return after + timedelta(minutes=5)
