"""Model Gateway with pluggable Provider Adapters.

Agent Runtime NEVER calls an LLM directly — always through ModelGateway.
每次调用自动记录 Token 用量流水（六维：公司/部门/个人/项目/任务/场景）。
"""
import contextlib
import time
from abc import ABC, abstractmethod

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import contextvars

from app.core.logging_config import get_logger
from app.models.agent import Model, ModelProviderType
from app.models.budget import TokenBudget
from app.models.token_usage import TokenUsage

logger = get_logger(__name__)

# 调用上下文（ContextVar 支持并发请求各自隔离）


class BudgetExhausted(RuntimeError):
    """V5.5 P0：token 预算到顶，自动暂停（不是加价惩罚）。

    老板在「Token 统计 → 预算上限」调高/关闭后立即恢复。
    """

usage_ctx: contextvars.ContextVar = contextvars.ContextVar("usage_ctx", default=None)


def set_usage_context(**kw):
    """设置本次调用的归属维度（遗留接口：只 set 不 reset）。

    保留是为了兼容旧调用方；新代码请用 with usage_context(...)，
    否则同一协程内多次调用会串上下文（agent/project 维度泄漏）。
    """
    usage_ctx.set(kw)


@contextlib.contextmanager
def usage_context(**kw):
    """with 块内设置本次调用的归属维度，退出时自动 reset。

    用法：
        with usage_context(agent=agent, scenario="MEETING", task_id=tid):
            await gateway.chat_with_agent_models(model_ids, messages)
    """
    token = usage_ctx.set(kw)
    try:
        yield kw
    finally:
        usage_ctx.reset(token)


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
        # 既支持传 agent 对象，也支持直接传 agent_id/agent_name/company_id
        #（training/factory 等只有 id 的路径）；显式 id 优先于对象属性。
        agent_id = (str(getattr(agent, "id", None) or "") or None
                    or (ctx.get("agent_id") or None))
        agent_name = getattr(agent, "name", None) or ctx.get("agent_name")
        company_id = (str(getattr(agent, "company_id", None) or "")
                      or ctx.get("company_id"))
        department_id = ((str(getattr(agent, "department_id", None) or "") or None)
                         or ctx.get("department_id"))

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
            # 只 add 不 commit：调用方传入的是业务会话，提交权交还外层事务
            # 统一提交，避免中途提交破坏外层事务原子性（审查 P0-②）。
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


def load_provider_plugins() -> dict[str, object]:
    """从 plugins/providers/*.py 动态加载第三方 provider 插件。

    约定：每个 py 文件暴露 `register()`，返回 provider 类或已实例化的对象
    （类须为 BaseModelProvider 子类并声明 provider_type）。单个文件加载
    失败只记 warning，不影响整体；内置 PROVIDERS 始终保留。
    返回 {provider_type_value: provider类或实例}。
    """
    import importlib.util
    from pathlib import Path

    loaded: dict[str, object] = {}
    # model_gateway.py 在 apps/api/app/ 下，插件目录在仓库根 plugins/
    plug_dir = Path(__file__).resolve().parent.parent.parent / "plugins" / "providers"
    if not plug_dir.is_dir():
        return loaded
    for py in sorted(plug_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"aieos_provider_plugin_{py.stem}", py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if not callable(register):
                logger.warning("provider 插件 %s 缺少 register()，跳过", py.name)
                continue
            obj = register()
            cls = obj if isinstance(obj, type) else type(obj)
            if not (isinstance(obj, type) and issubclass(obj, BaseModelProvider)):
                logger.warning("provider 插件 %s 返回的不是 BaseModelProvider 子类，跳过",
                               py.name)
                continue
            ptype = getattr(cls, "provider_type", None)
            key = getattr(ptype, "value", ptype)
            if key is None:
                logger.warning("provider 插件 %s 未声明 provider_type，跳过", py.name)
                continue
            loaded[str(key)] = obj
            logger.info("加载 provider 插件 %s -> %s", py.name, key)
        except Exception as e:  # 单个插件失败不影响整体
            logger.warning("provider 插件 %s 加载失败，已跳过：%s",
                           py.name, e, exc_info=True)
    return loaded


# 导入期加载一次：插件可新增 provider 类型，不覆盖内置 PROVIDERS
_PLUGIN_PROVIDERS = load_provider_plugins()


# ---------------- 简易熔断/冷却（内存实现，无 Redis 也可用） ----------------

# 每模型连续失败计数与冷却截止时间：key 为 Model.id
_FAIL_STREAK: dict[str, int] = {}
_COOL_UNTIL: dict[str, float] = {}

FAIL_STREAK_THRESHOLD = 5   # 连续失败达 5 次进入冷却
COOL_DOWN_SECONDS = 300     # 冷却 5 分钟


def _in_cooldown(model_id: str) -> bool:
    until = _COOL_UNTIL.get(model_id)
    if until and time.time() < until:
        return True
    _COOL_UNTIL.pop(model_id, None)
    return False


def _note_failure(model_id: str) -> None:
    streak = _FAIL_STREAK.get(model_id, 0) + 1
    _FAIL_STREAK[model_id] = streak
    if streak >= FAIL_STREAK_THRESHOLD:
        _COOL_UNTIL[model_id] = time.time() + COOL_DOWN_SECONDS
        logger.warning("模型 %s 连续失败 %d 次，进入 %d 秒冷却",
                       model_id, streak, COOL_DOWN_SECONDS)


def _note_success(model_id: str) -> None:
    _FAIL_STREAK.pop(model_id, None)
    _COOL_UNTIL.pop(model_id, None)


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    return code


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

    @staticmethod
    def _build_provider(row: Model):
        """按 provider 类型构造调用器：内置 PROVIDERS 优先，插件兜底。

        插件 register() 可返回类（按 base_url/api_key/model_id 实例化）
        或已实例化的对象；未知类型返回 None，由调用方记入失败摘要。
        """
        provider_cls = PROVIDERS.get(row.provider_type)
        if provider_cls is not None:
            return provider_cls(
                base_url=row.base_url or "",
                api_key=_resolve_api_key(row),
                model_id=row.model_id,
            )
        key = str(getattr(row.provider_type, "value", row.provider_type))
        plugin_obj = _PLUGIN_PROVIDERS.get(key)
        if plugin_obj is None:
            return None
        if isinstance(plugin_obj, type):
            return plugin_obj(
                base_url=row.base_url or "",
                api_key=_resolve_api_key(row),
                model_id=row.model_id,
            )
        return plugin_obj

    async def _enforce_budget(self, ctx: dict | None) -> None:
        """V5.5 P0 成本护栏：LLM 调用前检查 token 预算上限，到顶即抛暂停。"""
        if not ctx:
            return
        agent_obj = ctx.get("agent")
        company_id = ctx.get("company_id") or getattr(agent_obj, "company_id", None)
        agent_id = getattr(agent_obj, "id", None) or ctx.get("agent_id")
        if not company_id:
            return
        budgets = (await self.db.execute(
            select(TokenBudget).where(
                TokenBudget.company_id == company_id,
                TokenBudget.enabled == True))).scalars().all()  # noqa: E712
        if not budgets:
            return
        from datetime import datetime as _dt
        today_start = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for b in budgets:
            if b.scope_type == "agent" and b.scope_id != agent_id:
                continue
            q = select(
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
            ).where(TokenUsage.company_id == company_id)
            if b.scope_type == "agent" and b.scope_id:
                q = q.where(TokenUsage.agent_id == b.scope_id)
            if b.period == "day":
                q = q.where(TokenUsage.created_at >= today_start)
            row = (await self.db.execute(q)).one()
            tokens, cost = int(row[0]), float(row[1])
            exceeded = ((b.limit_tokens and tokens >= b.limit_tokens)
                        or (b.limit_cost and cost >= b.limit_cost))
            if not exceeded:
                continue
            if not b.paused:
                b.paused = True
                b.paused_reason = (f"用量到顶（{tokens}/"
                                    f"{'∞' if not b.limit_tokens else b.limit_tokens}"
                                    f" token, ¥{cost:.2f}/"
                                    f"{'∞' if not b.limit_cost else b.limit_cost:.2f}），已自动暂停")
                try:
                    await self.db.commit()
                except Exception:
                    pass
            raise BudgetExhausted(
                f"token 预算上限已触发，执行已自动暂停：已用 {tokens} token"
                f"（上限 {b.limit_tokens or '∞'}）。老板可在「Token 统计 → 预算上限」"
                f"调高额度或暂停该上限后立即恢复。")

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
                "再在员工详情页把它绑定为首选模型。")

        last_error: Exception | None = None
        ctx = usage_ctx.get()
        # V5.5 P0：预算上限预检查（到顶抛 BudgetExhausted，任务停下等老板调额）
        await self._enforce_budget(ctx)
        attempted = 0
        missing: list[str] = []
        failures: list[str] = []  # 每次失败的摘要，最终异常附全部（不再只留最后一次）
        for mid in model_ids:
            row = await self.db.get(Model, mid)
            if row is None:
                missing.append(mid)
                continue
            attempted += 1
            # 熔断：冷却期内的模型直接跳过，不浪费一次调用
            if _in_cooldown(mid):
                failures.append(f"{row.name}：熔断冷却中，已跳过")
                logger.warning("模型 %s(%s) 处于熔断冷却期，跳过本次调用",
                               row.name, mid)
                continue
            provider = self._build_provider(row)
            if provider is None:
                failures.append(f"{row.name}：未知 provider 类型 {row.provider_type}")
                logger.warning("模型 %s(%s) 的 provider 类型 %s 无法解析，跳过",
                               row.name, mid, row.provider_type)
                continue
            t0 = time.time()
            try:
                result = await provider.chat(messages, tools=tools, config=config)
                result["used_model_id"] = mid
                _note_success(mid)
                # ---- Token 计量（只 add 不 commit，外层事务统一提交）----
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
                err_summary = f"未实现：{e}"
                failures.append(f"{row.name}：{err_summary}")
                logger.warning("模型 %s(%s) 调用失败：%s", row.name, mid, err_summary)
                _note_failure(mid)
                continue
            except Exception as e:  # failover to next model
                last_error = e
                err_summary = f"{type(e).__name__}: {e}"
                failures.append(f"{row.name}：{err_summary}")
                logger.warning("模型 %s(%s) 调用失败：%s",
                               row.name, mid, err_summary)
                _note_failure(mid)
                # 失败调用也进 token 流水（success=False），metrics 成功率
                # 口径 = success 为真 / 总数
                try:
                    from app.model_gateway import record_token_usage
                    await record_token_usage(
                        self.db, {}, model_row=row, ctx=ctx,
                        latency_ms=int((time.time() - t0) * 1000),
                        success=False)
                except Exception as usage_err:
                    logger.error("失败调用计量落库失败 model=%s mid=%s: %s",
                                 row.name, mid, usage_err, exc_info=True)
                # HTTP 429（限流/配额耗尽）直接短路：不再逐个试后续模型
                if _http_status(e) == 429:
                    logger.warning("模型 %s(%s) 返回 429，直接短路结束 failover",
                                   row.name, mid)
                    break
                continue

        if attempted == 0:
            raise RuntimeError(
                f"该员工绑定的模型已不存在（可能已被删除）：{', '.join(missing[:3])}。"
                "请重新绑定一个可用模型。")
        raise RuntimeError(
            f"已绑定的 {attempted} 个模型全部调用失败：{'; '.join(failures)}")

    async def chat_with_agent_models_by_agent(self, agent, messages,
                                              tools=None):
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
