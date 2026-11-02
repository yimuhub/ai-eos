"""Agent digital employees + model configs (P1-04, P1-05)."""
import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (Boolean, DateTime, JSON, Enum, ForeignKey, String,
                        Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from .base import TimestampMixin

if TYPE_CHECKING:
    # 仅在类型检查期导入：Department 与本模型互相引用，
    # 运行时靠 SQLAlchemy registry 按类名解析字符串关系目标。
    # 显式声明可让 pyflakes / mypy 正确识别 Mapped["Department"]。
    from app.models.agent_group import AgentGroup
    from app.models.company import Department


class AgentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class ModelProviderType(str, enum.Enum):
    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class ModelType(str, enum.Enum):
    """模型用途。文本之外的类型不能当聊天模型绑定给员工。"""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    OTHER = "other"


class Model(TimestampMixin, Base):
    """A registered LLM endpoint (provider adapter config)."""
    __tablename__ = "models"

    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    provider_type: Mapped[ModelProviderType] = mapped_column(Enum(ModelProviderType, native_enum=False, length=50))
    base_url: Mapped[str | None] = mapped_column(String(500))
    api_key_env: Mapped[str | None] = mapped_column(String(200))
    model_id: Mapped[str] = mapped_column(String(200))  # e.g. agnes-2.5-flash
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)

    # --- 供应商与模型类型（全部可空：老库靠 ensure_columns 幂等补列） ---
    # 归属的供应商。NULL = 升级前登记的直连模型，前端以「旧版直连」展示。
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # text|image|video|audio|embedding|other。NULL 等价 text（升级前只有聊天模型）。
    model_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True)
    # 是否多模态（可接受/生成多种输入输出，如看图、生图）
    multimodal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 能力标签：chat / vision / tools / reasoning / image_generation / ...
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # NULL 等价 True：历史模型默认可用。
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    # auto（上游拉取）| manual（人工设置）| default（无上游信息时按类型补全）
    config_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 上游原始元数据（上下文长度、模态、定价等），供重新同步与排查
    upstream_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentModelConfig(TimestampMixin, Base):
    """Per-agent primary/secondary/fallback model binding."""
    __tablename__ = "agent_model_configs"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="primary")  # primary|secondary|fallback
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"))


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), index=True)
    # 部门下的组（CEO 按需招人时的落位层级）；NULL = 只归部门、不分组
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    avatar: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AgentStatus] = mapped_column(Enum(AgentStatus, native_enum=False, length=50), default=AgentStatus.IDLE)
    manager_agent_id: Mapped[str | None] = mapped_column(String(36))
    memory_config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    doc_style_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 绑定的文档风格
    # V5.0：绑定的专家库专家（expert_templates.id），激活时取其 system_prompt 作人设
    expert_template_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # --- P5 可见性 / 归属（全部可空：老库靠 ensure_columns 幂等补列） ---
    # company | department | private；NULL 等价 company（升级前的老 Agent 全员可见）
    visibility: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # 谁创建的（成员私有 Agent 的写权限依据）；NULL = 历史数据，只有 owner/admin 能写
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 预留：行级数据域约束（如只允许访问某类数据），语义由上层定义
    data_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 超级 Agent（CEO）：一个公司至多一个。可空是为了让老库幂等补列，
    # NULL 等价 False；切换 CEO 时由路由把旧的置 False（不删除，保留历史）。
    is_ceo: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    # 任命 CEO 前的姓名。任命时 name 改为统一显示名「CEO」，解除时按它还原；
    # 单独存列而不是塞进 data_scope：改名是可逆的身份状态，不是业务约束。
    pre_ceo_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    department: Mapped["Department | None"] = relationship(back_populates="agents")
    group: Mapped["AgentGroup | None"] = relationship(back_populates="agents")
    model_configs: Mapped[list["AgentModelConfig"]] = relationship(
        backref="agent", cascade="all, delete-orphan"
    )
