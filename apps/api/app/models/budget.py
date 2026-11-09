"""Token 预算上限（V5.5 P0：一人公司成本护栏）。

老板给每个维度设 token 上限，用量到顶 → 暂停（不是加价惩罚，
符合产品哲学）：
- ModelGateway 在每次 LLM 调用前检查；超额抛 BudgetExhausted；
- 任务引擎收到异常 → 任务停下（进 CEO 晨间简报"失败/卡住"提醒）；
- 老板随时可在「Token 统计」页调高/关闭预算，立即恢复。

scope:
- company：整家公司每日/累计 token 总闸
- agent：单个员工独立额度
"""
import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Boolean, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import iso_utc
from app.db.base import Base


def _now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id() -> str:
    return str(uuid.uuid4())


class TokenBudget(Base):
    """Token 用量上限。"""
    __tablename__ = "token_budgets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True,
                                   default=_new_id)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # scope_type: "company"（scope_id 空）或 "agent"（scope_id=员工id）
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False,
                                           default="company")
    scope_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 上限：total_tokens 或 cost（元）
    limit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 周期："day"（每天 0 点重置）或 "total"（累计）
    period: Mapped[str] = mapped_column(String(10), nullable=False, default="day")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 到顶后自动挂起标记（供前端显示"已暂停"）
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "company_id": self.company_id,
            "scope_type": self.scope_type, "scope_id": self.scope_id,
            "limit_tokens": self.limit_tokens, "limit_cost": self.limit_cost,
            "period": self.period, "enabled": self.enabled,
            "paused": self.paused, "paused_reason": self.paused_reason,
            "note": self.note,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }
