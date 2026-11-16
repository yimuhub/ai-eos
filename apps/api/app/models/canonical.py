"""数据规整层与指标层（V6 M3）：Canonical 实体、企业自定义字段、指标、同步账。

设计要点（docs/升级方案 §4.5）：
* 各来源（连接器/webhook/手工导入）先映射成**统一实体**再入库——
  这一层是"AI 能不能真干活"的分水岭：没有它，每个 Agent 都要面对
  N 套字段名。payload 存规范字段，custom_data 存企业自定义字段，
  raw 保留原始行（排障与回溯，可清）。
* 唯一键 (company, entity_type, source, external_id)：同一来源同一条
  外部记录幂等 upsert，重跑同步不翻倍。
* 指标是**结构化 spec**（实体+聚合+过滤），不是裸 SQL——AI 通过
  metrics.query 工具取数，永远不直接打业务库。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin

# 规范实体枚举（v1）：拓客/锁客/生产/财务/人力五域的最小覆盖
ENTITY_TYPES = ("contact", "account", "order", "product", "voucher",
                "employee", "workorder")


class CanonicalRecord(TimestampMixin, Base):
    """规整层：外部系统记录的统一实体形态。"""
    __tablename__ = "canonical_records"
    __table_args__ = (
        UniqueConstraint("company_id", "entity_type", "source", "external_id",
                         name="uq_canonical_source_id"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                             index=True)
    # 连接器 tool_registry 行 id、"manual" 或 "webhook:<id>"
    source: Mapped[str] = mapped_column(String(120), nullable=False,
                                        index=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    custom_data: Mapped[dict | None] = mapped_column(JSON, default=dict)
    raw: Mapped[dict | None] = mapped_column(JSON)
    # 源系统业务时间（排序/水位用）；为空时回退 updated_at
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class CustomFieldDef(TimestampMixin, Base):
    """企业自定义字段（M4 企业定制层的数据底座，规整层先引用）。"""
    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("company_id", "entity_type", "key",
                         name="uq_custom_field"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                             index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # text | number | date | select | boolean
    type: Mapped[str] = mapped_column(String(20), nullable=False,
                                      default="text")
    options: Mapped[list | None] = mapped_column(JSON, default=list)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                           default=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))


class MetricDef(TimestampMixin, Base):
    """指标注册表：口径 = 结构化 spec，AI 经 metrics.query 工具消费。"""
    __tablename__ = "metric_defs"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_metric_key"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # higher_better | lower_better
    direction: Mapped[str] = mapped_column(String(20), nullable=False,
                                           default="higher_better")
    description: Mapped[str | None] = mapped_column(Text)
    # {entity, op: count|sum|avg|min|max, field?, filters?: [{field,op,value}], group_by?}
    spec: Mapped[dict | None] = mapped_column(JSON, default=dict)
    required_capability: Mapped[str] = mapped_column(String(20),
                                                     nullable=False,
                                                     default="view")
    created_by: Mapped[str | None] = mapped_column(String(36))


class ConnectorSyncRun(TimestampMixin, Base):
    """连接器同步账：每次拉取一条，监控页与排障的事实源。"""
    __tablename__ = "connector_sync_runs"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    tool_id: Mapped[str] = mapped_column(String(36), nullable=False,
                                         index=True)
    # rest | webhook | mcp_bridge | file
    kind: Mapped[str] = mapped_column(String(20), nullable=False,
                                      default="rest")
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # running | success | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="running")
    records_upserted: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=0)
    watermark: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
