"""External Agent Integration (P3-09).

统一 Agent Adapter 接口 + 外部 Agent 注册中心。
API Key 不落明文：存 env 引用名，运行时解析。

## auth_env_var 的安全边界（修复「密钥外发」）

`auth_env_var` 原本可以是**任意**环境变量名，而 adapter 会把其值当作
`Authorization: Bearer` 发到同样由调用方指定的 `endpoint`。于是
`auth_env_var=JWT_SECRET` + `endpoint=https://attacker.tld` 就能窃取
JWT 签名密钥。现在改为固定允许列表（只认模型服务 key），并显式拒绝
服务端敏感变量；旧数据在解析/执行时同样会被拦下，而不是只在注册时拦。
"""
import enum
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.net_guard import validate_outbound_url_async
from app.db.base import Base


# 只允许引用这些「模型服务密钥」。新供应商接入时在此登记，
# 不允许通过用户输入扩展——环境变量名本身就是攻击面。
ALLOWED_AUTH_ENV_VARS = frozenset({
    "OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "MINIMAX_API_KEY",
    "SILICONFLOW_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "PERPLEXITY_API_KEY",
    "COHERE_API_KEY",
    "ARK_API_KEY",
    "VOLCENGINE_API_KEY",
})

# 即使是模型服务也不该碰的服务端配置；双保险，避免未来有人在
# 允许列表里误加（例如某个供应商的 key 恰好叫 *_SECRET）。
FORBIDDEN_AUTH_ENV_NAMES = frozenset({
    "JWT_SECRET",
    "SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "ENCRYPTION_KEY",
    "PRIVATE_KEY",
})
_FORBIDDEN_SUFFIXES = ("_SECRET", "_PASSWORD")


def is_forbidden_auth_env(name: str) -> bool:
    upper = (name or "").strip().upper()
    return (upper in FORBIDDEN_AUTH_ENV_NAMES
            or upper.endswith(_FORBIDDEN_SUFFIXES))


def validate_auth_env_var(name: str | None) -> str | None:
    """校验 auth_env_var：只允许模型服务 key 的白名单。非法则抛 ValueError。"""
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if is_forbidden_auth_env(upper):
        raise ValueError("该环境变量属于服务端敏感配置，禁止作为外部 Agent 密钥引用")
    if upper not in ALLOWED_AUTH_ENV_VARS:
        raise ValueError(
            "auth_env_var 只允许引用模型服务密钥（如 OPENAI_API_KEY / "
            "ANTHROPIC_API_KEY / GEMINI_API_KEY），其他变量名一律拒绝")
    # 返回规范名（环境变量大小写敏感，但白名单统一大写；常见部署均为大写）
    return upper


def resolve_auth_env(name: str | None) -> str | None:
    """按白名单解析密钥；非法名字抛 ValueError，取不到返回 None。"""
    validated = validate_auth_env_var(name)
    return os.environ.get(validated) if validated else None


async def validated_endpoint(conn: "ExternalAgentConnection") -> str:
    """校验并返回 endpoint（允许私网：本地 LLM；元数据地址仍拒绝）。"""
    endpoint = (conn.endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint 未配置")
    return await validate_outbound_url_async(endpoint, allow_private=True)


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
        # 不在白名单的环境变量在这里抛 ValueError（旧数据同样拦住）
        return resolve_auth_env(self.conn.auth_env_var)


class APIAgentAdapter(AgentAdapter):
    """OpenAI 兼容远程 API Adapter。"""

    async def execute(self, task_input: str, config: dict | None = None) -> dict:
        import httpx
        endpoint = await validated_endpoint(self.conn)
        headers = {}
        key = self._resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{endpoint}/chat/completions",
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
            endpoint = await validated_endpoint(self.conn)
            async with httpx.AsyncClient(timeout=10, trust_env=False,
                                         follow_redirects=False) as client:
                resp = await client.get(endpoint)
            return {"ok": True, "http": resp.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}


class LocalAgentAdapter(APIAgentAdapter):
    """本地 LLM（llama.cpp 等 OpenAI 兼容服务）。"""

    async def get_status(self) -> dict:
        return {"reachable": bool(self.conn.endpoint),
                "provider": "local", "hint": "llama.cpp OpenAI-compatible"}


class McpHttpAgentAdapter(AgentAdapter):
    """V5.5 B2 入站：外部 MCP Server（streamable-http / JSON-RPC POST）。

    execute(task_input):
      - JSON：{"tool": "...", "arguments": {...}} → 直通 tools/call
      - 自然语言 → 先 tools/list，取第一个工具执行（尽力而为）
    """

    async def execute(self, task_input: str, config: dict | None = None) -> dict:
        from app.api.routes.mcp import mcp_http_execute
        endpoint = await validated_endpoint(self.conn)
        return await mcp_http_execute(
            endpoint, task_input, config,
            bearer=self._resolve_api_key())

    async def get_status(self) -> dict:
        return {"reachable": bool(self.conn.endpoint), "provider": "mcp"}

    async def test_connection(self) -> dict:
        import httpx
        try:
            endpoint = await validated_endpoint(self.conn)
            headers = {}
            key = self._resolve_api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            async with httpx.AsyncClient(timeout=10, trust_env=False,
                                         follow_redirects=False) as client:
                r = await client.post(endpoint, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {}}, headers=headers)
                return {"ok": r.status_code < 500, "http": r.status_code,
                        "provider": "mcp"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:150]}


ADAPTERS = {
    AgentProviderType.API: APIAgentAdapter,
    AgentProviderType.LOCAL: LocalAgentAdapter,
    AgentProviderType.REMOTE: APIAgentAdapter,
    AgentProviderType.NATIVE: None,  # Native Agent 走内部 runtime，不走 adapter
}
# V5.5 B2：外部 MCP Server 接入（复用 REMOTE 类型的 endpoint + JSON 直通）
MCP_PROVIDER_ALIASES = {"MCP": McpHttpAgentAdapter}


def get_adapter(connection: ExternalAgentConnection) -> AgentAdapter:
    # V5.5 B2：REMOTE + capabilities 含 "mcp" → 外部 MCP Server 直通
    if (connection.provider_type == AgentProviderType.REMOTE
            and "mcp" in (connection.capabilities or [])):
        return McpHttpAgentAdapter(connection)
    cls = ADAPTERS.get(connection.provider_type)
    if cls is None:
        raise ValueError(f"{connection.provider_type} 不需要 adapter")
    return cls(connection)
