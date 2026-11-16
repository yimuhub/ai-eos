"""工具总线注册表：进程内的工具事实源 + 按 Agent 解析。

统一原则（docs/升级方案_万能基座V6 §4.1）：**一切工具皆协议**。

三层来源合并为模型可见的工具集：
1. in-code（`builtin.py` 注册）：web 工具人人都有（兼容现状）；
   computer_* 只在装了 computer_use 能力包时可见；
   mcp_server 类（aieos.*）只暴露给外部 Agent，不下发给自家员工；
2. 能力包：`capability_packs.resolve_capabilities` 列出的工具名，
   只有在注册表里**真的有执行器**才下发——修掉"提示词宣传了不存在的
   工具"这个幻觉源头；
3. DB 登记（`tool_registry` 行 + `agent_tool_grants` 授权）：外部 MCP、
   连接器、自定义工具，按公司隔离。

执行器统一签名：``async def(args: dict, ctx: ToolContext) -> dict``。
需要数据库的执行器自己开短会话（见 `executors.py` / dispatch 的审计），
不复用调用方事务——任务链的工具轮会跨 LLM 调用持有写锁（B-080 教训），
工具执行绝不能再加重它。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 单次工具结果回填上限：工具结果会和历史消息一起进下一轮 prompt，
# 不截断会让上下文迅速膨胀（一次抓取 8000 字 × 多轮）。
MAX_TOOL_RESULT_CHARS = 6000
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 12


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的上下文（审计与执行都要带上）。

    tool_name 由 dispatch 填充：执行器签名只收 (args, ctx)，
    靠它才知道自己在替哪个工具干活（如 computer_* 剥前缀得 capability）。
    """
    company_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    tool_name: str | None = None


# 执行器签名：args = 模型给的参数；ctx = 调用上下文
ToolExecutor = Callable[[dict, ToolContext], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    """一个可被模型调用的工具的完整描述（代码侧事实源）。"""
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object",
                                                      "properties": {}})
    # builtin | computer | skill | mcp_server | db
    source: str = "builtin"
    # low | medium | high —— 高风险工具由 computer_service 的审批门/预算管
    risk: str = "low"
    required_capability: str = "view"
    category: str | None = None
    display_name: str | None = None
    # 给模型的工具使用纪律片段（可选；拼在工具清单旁）
    prompt_hint: str | None = None
    # 是否暴露给外部 Agent（MCP Server 的 tools/list）
    expose_mcp: bool = False
    executor: ToolExecutor | None = None


_SPECS: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec, *, replace: bool = False) -> None:
    """注册一个工具。重名默认抛错——在 import 期炸掉比在运行期静默覆盖好。

    测试要覆盖同名的场景显式传 replace=True。
    """
    if spec.name in _SPECS and not replace:
        raise RuntimeError(f"工具重名注册被拒绝: {spec.name}")
    _SPECS[spec.name] = spec


def replace_tool(spec: ToolSpec) -> None:
    _SPECS[spec.name] = spec


def get_tool(name: str) -> ToolSpec | None:
    return _SPECS.get(name)


def all_incode_tools() -> list[ToolSpec]:
    return list(_SPECS.values())


# ---------------------------------------------------------------------------
# schema 形态转换：一套事实源，两种协议形态
# ---------------------------------------------------------------------------

def openai_schema(spec: ToolSpec) -> dict:
    """OpenAI function-calling 形态（喂 native 链路）。"""
    return {"type": "function",
            "function": {"name": spec.name,
                         "description": spec.description,
                         "parameters": spec.parameters}}


def mcp_descriptor(spec: ToolSpec) -> dict:
    """MCP tools/list 形态（喂外部 Agent）。"""
    return {"name": spec.name, "description": spec.description,
            "inputSchema": spec.parameters}


def tool_schemas(specs: list[ToolSpec]) -> list[dict]:
    return [openai_schema(s) for s in specs]


def mcp_descriptors(specs: list[ToolSpec]) -> list[dict]:
    return [mcp_descriptor(s) for s in specs]


# ---------------------------------------------------------------------------
# DB 动态工具：tool_registry 行 ↔ ToolSpec
# ---------------------------------------------------------------------------

def spec_from_row(row) -> ToolSpec:
    """把 tool_registry 行转成运行时 spec。

    executor 恒为 None：DB 工具的执行由 dispatch 按 `source` 路由
    （mcp_http 直通 / connector SDK / 自定义），不在 spec 里绑函数。
    """
    return ToolSpec(
        name=row.name,
        description=row.description or row.display_name or row.name,
        parameters=row.parameters if isinstance(row.parameters, dict)
        and row.parameters else {"type": "object", "properties": {}},
        source="db",
        risk=row.risk or "low",
        required_capability=row.required_capability or "operate",
        category=row.category,
        display_name=row.display_name,
        executor=None,
    )


async def resolve_agent_tools(db: AsyncSession, agent) -> list[ToolSpec]:
    """解析某个 Agent 真正可用（且真的可执行）的工具集。

    返回的每一项都能被 dispatch 执行——这是"模型看到的 = 真的能用"的
    承诺。不可执行的宣传名（如 memory_write）永远不进 schema。
    """
    from app.capability_packs import resolve_capabilities

    rc = resolve_capabilities(getattr(agent, "memory_config", None))
    specs: dict[str, ToolSpec] = {}
    for spec in all_incode_tools():
        if spec.source == "builtin":
            specs[spec.name] = spec  # web 工具：所有 Agent 兜底可用（现状）
        elif spec.source == "computer" and spec.name in rc.tools:
            specs[spec.name] = spec
        # mcp_server（aieos.*）不下发给自家员工——那是给外部 Agent 的面

    from app.models.toolbus import AgentToolGrant, ToolRegistry
    grant_names = (await db.execute(
        select(AgentToolGrant.tool_name)
        .where(AgentToolGrant.agent_id == agent.id)
    )).scalars().all()
    grant_names = [n for n in grant_names if n]
    if grant_names:
        rows = (await db.execute(
            select(ToolRegistry).where(
                ToolRegistry.company_id == agent.company_id,
                ToolRegistry.enabled.is_(True),
                ToolRegistry.name.in_(grant_names))
        )).scalars().all()
        for row in rows:
            specs[row.name] = spec_from_row(row)
    return list(specs.values())


async def resolve_agent_tool_schemas(db, agent) -> list[dict]:
    return tool_schemas(await resolve_agent_tools(db, agent))


def tool_prompt_section(specs: list[ToolSpec]) -> str:
    """工具清单的提示词片段（工具名 + 一句话用途），不可执行的不出现。"""
    if not specs:
        return ""
    lines = [f"- {s.name}：{(s.display_name or s.description)[:60]}"
             for s in specs]
    return "## 可用工具\n" + "\n".join(lines)


async def has_db_tool(db: AsyncSession, company_id: str | None,
                      name: str) -> bool:
    """公司里是否登记了某个动态工具（聊天链路的派发判断用）。"""
    if not company_id:
        return False
    from app.models.toolbus import ToolRegistry
    row = (await db.execute(
        select(ToolRegistry.id).where(
            ToolRegistry.company_id == company_id,
            ToolRegistry.name == name).limit(1))).first()
    return row is not None
