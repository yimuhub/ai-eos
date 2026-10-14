"""Token Usage Metering & Billing Models.

所有 LLM 调用经 ModelGateway 时自动落一条流水，维度齐全：
company / department / agent(个人) / project / task / scenario(学习·会议·工厂...)

计费标准（PricingConfig）后台可配：按量 or 订阅制，单价元/百万token。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


# ---------- 计费标准 ----------

class PricingConfig(Base):
    """每个模型一条计费标准；billing_mode: USAGE(按量) / SUBSCRIPTION(订阅)。"""
    __tablename__ = "pricing_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    model_name: Mapped[str | None] = mapped_column(String(120))       # 冗余展示名
    billing_mode: Mapped[str] = mapped_column(String(20), default="USAGE", nullable=False)
    input_price_per_m: Mapped[float] = mapped_column(Float, default=0.0)   # 元/百万输入token
    output_price_per_m: Mapped[float] = mapped_column(Float, default=0.0)  # 元/百万输出token
    monthly_fee: Mapped[float] = mapped_column(Float, default=0.0)         # 订阅包月费
    overage_input_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    overage_output_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    included_tokens_per_month: Mapped[int] = mapped_column(Integer, default=0)  # 订阅含 token 额度
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---------- Token 使用流水 ----------

class TokenUsage(Base):
    """每次 LLM 调用一行。六维：company/department/agent/project/task/scenario。

    cost 按当时生效的 PricingConfig 计算（快照单价），事后改价不影响历史。
    """
    __tablename__ = "token_usages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
    department_id: Mapped[str | None] = mapped_column(String(36), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True)     # 个人维度
    agent_name: Mapped[str | None] = mapped_column(String(120))              # 冗余
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)      # 操作人
    project_id: Mapped[str | None] = mapped_column(String(36), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    scenario: Mapped[str] = mapped_column(String(40), default="OTHER", index=True)
    # 场景枚举值: CHAT/MEETING/TASK/RUNTIME/FACTORY/TRAINING/LEARNING/
    #            REFLECTION/ORCHESTRATION/TEAM_GEN/SKILL/OTHER

    model_id: Mapped[str | None] = mapped_column(String(36))
    model_name: Mapped[str | None] = mapped_column(String(120))              # 实际用的模型
    provider_type: Mapped[str | None] = mapped_column(String(20))

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    input_cost: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    billing_mode_snapshot: Mapped[str | None] = mapped_column(String(20))

    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 备注/上下文摘要

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


Index("ix_token_usage_company_time", TokenUsage.company_id, TokenUsage.created_at)
Index("ix_token_usage_agent_time", TokenUsage.agent_id, TokenUsage.created_at)
