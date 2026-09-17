"""Model Gateway with pluggable Provider Adapters.

Agent Runtime NEVER calls an LLM directly — always through ModelGateway.
每次调用自动记录 Token 用量流水（六维：公司/部门/个人/项目/任务/场景）。
"""
import time
from abc import ABC, abstractmethod

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

import contextvars

from app.core.logging_config import get_logger
from app.models.agent import Model, ModelProviderType

logger = get_logger(__name__)

# 调用上下文（ContextVar 支持并发请求各自隔离）

usage_ctx: contextvars.ContextVar = contextvars.ContextVar("usage_ctx", default=None)


def set_usage_context(**kw):
    """设置本次调用的归属维度。业务代码在调 LLM 前 set，如：
    set_usage_context(agent=agent_obj, project_id="..", task_id="..", scenario="MEETING")
    """
    usage_ctx.set(kw)


def _get_ctx_value(ctx: dict, key: str):
    if not ctx:
        return None
    v = ctx.get(key)
    if v is None:
        return None
    # 支持 agent 对象或裸值
    if key == "agent":
        return v
    return v


async def record_token_usage(db, result: dict, model_row=None, ctx: dict | None = None,
                             latency_ms: int = 0, success: bool = True):
    """把一次 LLM 调用写入 token_usages 流水表（含费用快照）。失败不影响主流程。"""
    try:
        from sqlalchemy import select
        from app.models.token_usage import PricingConfig, TokenUsage, new_id
        usage = (result or {}).get("usage") or {}
        inp = int(usage.get("prompt_tokens") or 0)
        out = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or inp + out)

        ctx = ctx or {}
        agent = ctx.get("agent")
        agent_id = str(getattr(agent, "id", None) or "") or None
        agent_name = getattr(agent, "name", None)
        company_id = str(getattr(agent, "company_id", None) or "") or ctx.get("company_id")
        department_id = str(getattr(agent, "department_id", None) or "") or None

        model_id = str(getattr(model_row, "id", None) or "") or None
        model_name = getattr(model_row, "name", None) or (result or {}).get("raw_model")
        provider_type = str(getattr(model_row, "provider_type", "") or "").split(".")[-1][:20] or None

        # 计费：找该模型的定价配置（enabled 的第一条）
        pc = None
        if db is not None and model_id:
            q = await db.execute(select(PricingConfig).where(
                PricingConfig.model_id == model_id, PricingConfig.enabled.is_(True)))
            pc = q.scalars().first()
        mode = (pc.billing_mode if pc else "USAGE")
        in_cost = out_cost = 0.0
        if mode == "USAGE" and pc:
            in_cost = round(inp / 1_000_000 * (pc.input_price_per_m or 0), 6)
            out_cost = round(out / 1_000_000 * (pc.output_price_per_m or 0), 6)
        elif mode == "SUBSCRIPTION" and pc:
            # 简化模型：超额部分按超额单价计；包含额度按月扣不在此摊
            over_in = max(0.0, (pc.input_price_per_m or 0) - (pc.overage_input_per_m or 0))
            over_out = max(0.0, (pc.output_price_per_m or 0) - (pc.overage_output_per_m or 0))
            in_cost = round(inp / 1_000_000 * over_in, 6)
            out_cost = round(out / 1_000_000 * over_out, 6)
        elif not pc and (result or {}).get("usage"):
            pass  # 未配置价格 → 记 tokens 不记钱

        row = TokenUsage(
            id=new_id(),
            company_id=str(company_id) if company_id else None,
            department_id=str(department_id) if department_id else None,
            agent_id=agent_id,
            agent_name=agent_name,
            user_id=(str(ctx.get("user_id")) if ctx.get("user_id") else None),
            project_id=(str(ctx.get("project_id")) if ctx.get("project_id") else None),
            task_id=(str(ctx.get("task_id")) if ctx.get("task_id") else None),
            scenario=(ctx.get("scenario") or "OTHER"),
            model_id=model_id,
            model_name=model_name,
            provider_type=provider_type,
            input_tokens=inp,
            output_tokens=out,
            total_tokens=total,
            input_cost=in_cost,
            output_cost=out_cost,
            total_cost=round(in_cost + out_cost, 6),
            billing_mode_snapshot=mode,
            latency_ms=latency_ms,
            success=success,
            detail=ctx.get("detail"),
        )
        if db is not None:
            db.add(row)
            await db.commit()
    except Exception as e:  # 计量绝不阻断主流程
        import logging
        logging.getLogger(__name__).warning(f"token metering failed: {e}")


class BaseModelProvider(ABC):
    """Provider Adapter interface — everything is a plugin."""

    provider_type: ModelProviderType

    @abstractmethod
    async def chat(self, messages: list[dict], tools=None, config: dict | None = None) -> dict:
        """Return {"content": str, "usage": {...}, "raw": ...}."""


class OpenAICompatProvider(BaseModelProvider):
    """Works for OpenAI / DeepSeek / Qwen / agnes / llama.cpp / any OpenAI-compatible API."""

    provider_type = ModelProviderType.OPENAI_COMPAT

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id

    async def chat(self, messages: list[dict], tools=None, config: dict | None = None) -> dict:
        cfg = config or {}
        body = {
            "model": self.model_id,
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": cfg.get("max_tokens", 8192),
        }
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        return {
            "content": choice["message"].get("content") or "",
            "tool_calls": choice["message"].get("tool_calls"),
            "usage": data.get("usage", {}),
            "raw_model": data.get("model", self.model_id),
        }


class AnthropicProvider(BaseModelProvider):
    """Skeleton adapter — implement when Anthropic API key available."""

    provider_type = ModelProviderType.ANTHROPIC

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self.base_url = base_url
        self.api_key = api_key
        self.model_id = model_id

    async def chat(self, messages, tools=None, config=None):
        raise NotImplementedError("AnthropicProvider adapter not yet implemented")


class GeminiProvider(BaseModelProvider):
    """Skeleton adapter."""

    provider_type = ModelProviderType.GEMINI

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self.base_url = base_url
        self.api_key = api_key
        self.model_id = model_id

    async def chat(self, messages, tools=None, config=None):
        raise NotImplementedError("GeminiProvider adapter not yet implemented")


PROVIDERS: dict[ModelProviderType, type[BaseModelProvider]] = {
    ModelProviderType.OPENAI_COMPAT: OpenAICompatProvider,
    ModelProviderType.ANTHROPIC: AnthropicProvider,
    ModelProviderType.GEMINI: GeminiProvider,
}


def _resolve_api_key(model_row: Model) -> str:
    """解析模型密钥。

    改造前这里直接读 os.environ，而 .env 从未被注入 os.environ，
    导致"只在 .env 里配了密钥"时静默取到空串、调用方收到 401 却查不出原因。
    现在统一走 settings.resolved_api_key（.env 已在 core.config 导入期注入）。
    """
    from app.core.config import settings

    provider_name = getattr(model_row.provider_type, "value", "") or ""
    return settings.resolved_api_key(model_row.api_key_env, provider_name)


class ModelGateway:
    """Single entry point: resolve agent model binding -> provider -> chat()."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def chat_with_agent_models(
        self,
        model_ids: list[str],
        messages: list[dict],
        tools=None,
        config: dict | None = None,
    ) -> dict:
        """Try models in priority order (primary -> secondary -> fallback).
        每次成功调用自动记录 Token 用量流水（上下文来自 set_usage_context）。

        错误信息必须**可操作**：原实现在没有绑定任何模型时会抛出
        `All configured models failed: None`——既没说明原因，也没告诉用户
        该做什么。端到端试跑正是靠断言"错误里必须出现『模型』"抓到它的。
        """
        if not model_ids:
            raise RuntimeError(
                "该员工尚未绑定任何模型，无法执行任务。"
                "请到「模型配置」登记一个模型并配置密钥，"
                "再在员工详情页把它绑定为 primary。")

        last_error: Exception | None = None
        ctx = usage_ctx.get()
        attempted = 0
        missing: list[str] = []
        for mid in model_ids:
            row = await self.db.get(Model, mid)
            if row is None:
                missing.append(mid)
                continue
            attempted += 1
            provider_cls = PROVIDERS[row.provider_type]
            provider = provider_cls(
                base_url=row.base_url or "",
                api_key=_resolve_api_key(row),
                model_id=row.model_id,
            )
            t0 = time.time()
            try:
                result = await provider.chat(messages, tools=tools, config=config)
                result["used_model_id"] = mid
                # ---- Token 计量（异步落库，不阻断）----
                try:
                    from app.model_gateway import record_token_usage
                    await record_token_usage(
                        self.db, result, model_row=row, ctx=ctx,
                        latency_ms=int((time.time() - t0) * 1000))
                except Exception as usage_err:
                    # 计量落库失败不阻断本次调用，但会导致用量/成本统计缺口，
                    # 属于财务口径问题，必须留痕而不是静默丢弃
                    logger.error("Token 用量落库失败 model=%s mid=%s: %s",
                                 row.name, mid, usage_err, exc_info=True)
                return result
            except NotImplementedError as e:
                last_error = e
                continue
            except Exception as e:  # failover to next model
                last_error = e
                continue

        if attempted == 0:
            raise RuntimeError(
                f"该员工绑定的模型已不存在（可能已被删除）：{', '.join(missing[:3])}。"
                "请重新绑定一个可用模型。")
        raise RuntimeError(
            f"已绑定的 {attempted} 个模型全部调用失败，最后一次错误：{last_error}")


async def chat_with_agent_models_by_agent(self, agent, messages, tools=None):
    """按 Agent 的模型配置（primary→secondary→fallback）调用。"""
    from sqlalchemy import select
    from app.models.agent import AgentModelConfig
    cfg_rows = (await self.db.execute(select(AgentModelConfig).where(
        AgentModelConfig.agent_id == str(agent.id)))).scalars().all()
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    model_ids = [c.model_id for c in sorted(cfg_rows,
                key=lambda c: order.get(c.role, 3))]
    if not model_ids:
        raise RuntimeError(f"Agent {agent.name} 未配置模型")
    return await self.chat_with_agent_models(model_ids, messages, tools=tools)


ModelGateway.chat_with_agent_models_by_agent = chat_with_agent_models_by_agent
