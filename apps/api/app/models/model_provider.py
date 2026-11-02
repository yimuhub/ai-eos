"""模型供应商：一个可反复添加的上游端点（含地址、密钥引用与默认配置）。

与 `Model` 的关系是「一个供应商 → N 个模型」：供应商保存连通信息，
模型只保存自己的标识、类型与调用配置。升级前 `Model` 自带 base_url /
api_key_env，没有供应商这一层；那一批行的 `provider_id` 为 NULL，
前端作为「旧版直连」展示，不强行改写。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .agent import ModelProviderType
from .base import TimestampMixin


class ModelProvider(TimestampMixin, Base):
    __tablename__ = "model_providers"

    # NULL = 升级前遗留的共享配置（与 models.company_id 同口径）
    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    provider_type: Mapped[ModelProviderType] = mapped_column(
        Enum(ModelProviderType, native_enum=False, length=50))
    base_url: Mapped[str | None] = mapped_column(String(500))
    # 密钥只存变量名；值在本机 .env，由 /model-providers/{id}/key 写入
    api_key_env: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    # 供应商级默认值：鉴权方式、额外请求头、超时、模型列表路径等
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)

    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
