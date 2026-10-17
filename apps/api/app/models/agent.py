"""Agent digital employees + model configs (P1-04, P1-05)."""
import enum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from .base import TimestampMixin

if TYPE_CHECKING:
    # 仅在类型检查期导入：Department 与本模型互相引用，
    # 运行时靠 SQLAlchemy registry 按类名解析字符串关系目标。
    # 显式声明可让 pyflakes / mypy 正确识别 Mapped["Department"]。
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

    department: Mapped["Department | None"] = relationship(back_populates="agents")
    model_configs: Mapped[list["AgentModelConfig"]] = relationship(
        backref="agent", cascade="all, delete-orphan"
    )
