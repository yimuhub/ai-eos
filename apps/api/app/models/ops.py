"""商业交付三引擎（V6.1）：合规台账、工资/分佣规则、对账。

设计依据 docs/二次调研_商业交付差距清单_2026-11-17.md：
* P0-1 证照合规台账——每行业都有罚则级证照与年审规则（食安法53条/病历
  保存年限/工资专户30日/预收费≤3个月/特种设备定检/三证年审），台账 +
  到期预警是「尽职免责」的产品化；
* P1-5 工资/分佣引擎——计件/计时/提成/分佣四种模式覆盖制造计件、建筑
  专户、教培课酬、物流承包、房产 ACN 的「老板算得清、发得出、有留痕」；
* P0-2 对账引擎——规整层两源比对（如 vouchers↔银行流水），差异项驱动
  催收/整改工作流。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class ComplianceLedger(TimestampMixin, Base):
    """证照/合规事项台账：行业包安装时按 compliance_items 播种。

    status 是派生态（服务层 refresh 后落库）：valid / expiring / expired /
    manual。到期预警由 automation 挂 refresh + briefing 消费。
    """
    __tablename__ = "compliance_ledgers"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_compliance_key"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    authority: Mapped[str | None] = mapped_column(String(200))
    # valid | expiring | expired | manual（manual = 无有效期，仅登记）
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="manual", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    renewal_rule: Mapped[str | None] = mapped_column(Text)
    renewal_days_before: Mapped[int] = mapped_column(Integer, nullable=False,
                                                     default=30)
    penalty: Mapped[str | None] = mapped_column(Text)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    # 责任人（数字员工 id）：到期预警派给谁跟进
    owner_agent_id: Mapped[str | None] = mapped_column(String(36))
    pack_id: Mapped[str | None] = mapped_column(String(64), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str | None] = mapped_column(String(36))


class PayrollRule(TimestampMixin, Base):
    """工资/分佣规则：口径存 spec，月度核算走 evaluate。

    spec: {mode: piece|hourly|commission|bonus,
           entity: "workorder",            # 计量来源实体（canonical）
           worker_field: "payload.worker", # 按谁聚合
           unit_field: "payload.qty",      # piece=件数 / hourly=小时
           rate: 1.5,                      # 单价（元/件、元/小时）
           amount_field: "payload.amount", # commission=提成基数
           rate_field: "payload.rate",     # 提成比例字段（可选，覆盖 rate）
           filters: [...],                 # 同指标层过滤
           bonus_fixed: 0}                 # bonus 固定加成
    """
    __tablename__ = "payroll_rules"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_payroll_key"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # piece | hourly | commission | bonus
    mode: Mapped[str] = mapped_column(String(20), nullable=False,
                                      default="piece")
    spec: Mapped[dict | None] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))


class ReconciliationRun(TimestampMixin, Base):
    """对账：规整层两个来源按匹配键比对，差异留档驱动后续动作。

    config: {entity: "order", source_a: "<tool_row_id|manual>",
             source_b: "<tool_row_id|manual>",
             match_field: "payload.order_no",   # 两边各取该字段做键
             compare_fields: ["payload.amount", "payload.status"],
             amount_tolerance: 0.01}
    """
    __tablename__ = "reconciliation_runs"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # running | success | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="running")
    total_a: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_b: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A 有 B 无 / B 有 A 无 / 两边都有但字段不一致
    only_a: Mapped[list | None] = mapped_column(JSON, default=list)
    only_b: Mapped[list | None] = mapped_column(JSON, default=list)
    mismatched: Mapped[list | None] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str | None] = mapped_column(String(36))
