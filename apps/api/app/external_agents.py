"""External Agent Integration (P3-09).

统一 Agent Adapter 接口 + 外部 Agent 注册中心。
API Key 不落明文：存 env 引用名，运行时解析。
"""
import enum
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentProviderType(str, enum.Enum):
    NATIVE = "NATIVE"          # AI-EOS 内建 Agent
    API = "API"                # OpenAI 兼容远程 API
    LOCAL = "LOCAL"            # 本地 LLM（如 llama.cpp）
    REMOTE = "REMOTE"          # 其他第三方


class ConnectionStatus(str, enum.Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class ExternalAgentConnection(Base):
    __tablename__ = "external_agent_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[AgentProviderType] = mapped_column(
        Enum(AgentProviderType), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    # Secret Management：只存环境变量名，不存明文 key
    auth_env_var: Mapped[str | None] = mapped_column(String(100))
    capabilities: Mapped[list | None] = mapped_column(JSON, default=list)
    supported_actions: Mapped[list | None] = mapped_column(JSON, default=list)
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus), nullable=False,
        default=ConnectionStatus.DISABLED, index=True)
    last_test_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用


# ---------- Adapter 抽象接口（所有 Provider 必须实现） ----------

class AgentAdapter(ABC):
    """统一 Agent Runtime Interface。业务代码禁止直接绑定具体 Provider。"""

    def __init__(self, connection: ExternalAgentConnection):
        self.conn = connection

    @abstractmethod
    async def execute(self, task_input: str, config: dict | None = None) -> dict:
        ...

    @abstractmethod
    async def get_status(self) -> dict:
        ...

    async def pause(self) -> dict:
        return {"paused": False, "reason": "unsupported"}

    async def resume(self) -> dict:
        return {"resumed": False, "reason": "unsupported"}

    async def cancel(self) -> dict:
        return {"cancelled": False, "reason": "unsupported"}

    def get_capabilities(self) -> list:
        return self.conn.capabilities or []

    def _resolve_api_key(self) -> str | None:
        import os
        if not self.conn.auth_env_var:
            return None
        return os.environ.get(self.conn.auth_env_var)


class APIAgentAdapter(AgentAdapter):
    """OpenAI 兼容远程 API Adapter。"""

    async def execute(self, task_input: str, config: dict | None = None) -> dict:
        import httpx
        headers = {}
        key = self._resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.conn.endpoint}/chat/completions",
                json={"messages": [{"role": "user", "content": task_input}],
                      **(config or {})},
                headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return {"output": data["choices"][0]["message"]["content"],
                "raw_usage": data.get("usage")}

    async def get_status(self) -> dict:
        ok = bool(self.conn.endpoint and self.conn.status == ConnectionStatus.ENABLED)
        return {"reachable": ok, "provider": "api"}

    async def test_connection(self) -> dict:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.conn.endpoint or "")
            return {"ok": True, "http": resp.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}


class LocalAgentAdapter(APIAgentAdapter):
    """本地 LLM（llama.cpp 等 OpenAI 兼容服务）。"""

    async def get_status(self) -> dict:
        return {"reachable": bool(self.conn.endpoint),
                "provider": "local", "hint": "llama.cpp OpenAI-compatible"}


ADAPTERS = {
    AgentProviderType.API: APIAgentAdapter,
    AgentProviderType.LOCAL: LocalAgentAdapter,
    AgentProviderType.REMOTE: APIAgentAdapter,
    AgentProviderType.NATIVE: None,  # Native Agent 走内部 runtime，不走 adapter
}


def get_adapter(connection: ExternalAgentConnection) -> AgentAdapter:
    cls = ADAPTERS.get(connection.provider_type)
    if cls is None:
        raise ValueError(f"{connection.provider_type} 不需要 adapter")
    return cls(connection)
