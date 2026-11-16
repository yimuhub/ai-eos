"""工具总线（V6 M1）：动态工具登记与 Agent 授权。

统一原则（docs/升级方案 §4.1）：一切工具皆协议——无论来自内置函数、技能、
能力包 sidecar、连接器还是外部 MCP Server，统一注册、统一描述、统一授权、
统一审计。

两张表的分工：
* `tool_registry` —— **动态**工具的运行时登记（外部 MCP、连接器、自定义）。
  内置工具不落库：代码即事实源（`app/toolbus/builtin.py`），避免测试依赖
  种子数据，也让存量测试保持确定性。
* `agent_tool_grants` —— 按 Agent 的授权。存 tool_name 而非外键：
  工具重登记/换版本不会破坏授权关系（与 agent_skills 存 id 的差异在于
  工具是按名字进模型上下文的，名字变了模型也认不得）。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class ToolRegistry(TimestampMixin, Base):
    """公司内登记的动态工具（外部 MCP / 连接器 / 自定义）。

    `endpoint` 只对 mcp_http 直通类工具有意义；凭据**绝不**明文入库——
    `auth_secret_ref` 指向 company_secret 里的键名，取值走 secret_vault。
    """
    __tablename__ = "tool_registry"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_tool_company_name"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    # 进模型 function-calling 的名字，须与技能 slug 不冲突（注册时校验）
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # mcp_http | mcp_stdio | connector | custom
    source: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="custom", index=True)
    source_ref: Mapped[str | None] = mapped_column(String(200))
    package_key: Mapped[str | None] = mapped_column(String(100), index=True)
    category: Mapped[str | None] = mapped_column(String(40))
    # OpenAI function-calling 的 parameters JSON Schema
    parameters: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # low | medium | high
    risk: Mapped[str] = mapped_column(String(10), nullable=False, default="low")
    required_capability: Mapped[str] = mapped_column(String(20), nullable=False,
                                                     default="operate")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False,
                                                 default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    auth_secret_ref: Mapped[str | None] = mapped_column(String(120))
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # ok | error | unknown
    health_status: Mapped[str | None] = mapped_column(String(20))
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_health_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))


class AgentToolGrant(TimestampMixin, Base):
    """哪个 Agent 被授予哪些工具（内置 + 动态统一按名字授权）。"""
    __tablename__ = "agent_tool_grants"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_name", name="uq_tool_grant_agent"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(36))
