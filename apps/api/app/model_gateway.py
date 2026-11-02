"""Model Gateway with pluggable Provider Adapters.

Agent Runtime NEVER calls an LLM directly — always through ModelGateway.
每次调用自动记录 Token 用量流水（六维：公司/部门/个人/项目/任务/场景）。
"""
import asyncio
import contextlib
import json
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from functools import lru_cache

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import contextvars

from app.core.logging_config import get_logger
from app.core.metrics import (
    LLM_DURATION_SECONDS,
    LLM_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
)
from app.db.session import AsyncSessionLocal
from app.models.agent import Model, ModelProviderType
from app.models.budget import TokenBudget
from app.models.token_usage import TokenUsage

logger = get_logger(__name__)

# ---------------- 共享 HTTP client（连接复用 + 有界连接池） ----------------

# 分阶段超时：建连 5s、读取 120s、写入 30s、等连接池 5s。
# 之前用单一 timeout=300，慢模型会长时间占住 worker 线程；
# 分阶段后连接故障会快速失败，真正生成阶段仍给足 120s。
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
HTTP_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=10)

# 仅对超时 / 5xx / 429 做有限重试：2 次重试（共 3 次尝试），
# 指数退避 + jitter，避免故障模型在瞬间把备用模型也打爆。
MAX_HTTP_RETRIES = 2
RETRY_BASE_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 8.0
# 429 是限流，408/425 是"稍后重试"语义，与 5xx 一起视为可重试
RETRYABLE_STATUS = frozenset({408, 425, 429})


@lru_cache(maxsize=1)
def get_http_client() -> httpx.AsyncClient:
    """进程级共享 AsyncClient（懒创建）。

    每次调用都新建 AsyncClient 会让连接无法复用、也失去了连接数上限；
    这里用单例把连接池收敛到 HTTP_LIMITS。
    """
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS)


def reset_http_client() -> None:
    """丢弃缓存的 client（仅供测试/事件循环切换时使用）。

    调用方应先 ``await aclose()`` 关闭旧 client，再重置。
    """
    get_http_client.cache_clear()


async def aclose() -> None:
    """关闭共享 client。

    main.py 的 lifespan 不在本工包改动范围内，因此这里暴露一个显式的
    异步关闭入口；接入 lifespan 时调用 ``await model_gateway.aclose()``
    即可。测试里先 ``await aclose()`` 再 ``reset_http_client()``。
    """
    if get_http_client.cache_info().currsize:
        client = get_http_client()
        get_http_client.cache_clear()
        await client.aclose()


def _is_retryable_status(code: int) -> bool:
    return code in RETRYABLE_STATUS or code >= 500


class UpstreamAPIError(httpx.HTTPStatusError):
    """上游 4xx/5xx，错误消息里带响应体摘要。

    ``raise_for_status()`` 只留一句 "Client error '400 Bad Request' for url ..."，
    用户拿着这句话没有任何可操作信息（是模型名错了、参数不支持，还是上下文
    超限？）。这里把上游原文脱敏、截断后附进消息，既能自查也不泄露密钥。
    """

    def __init__(self, message: str, *, request, response, excerpt: str = ""):
        super().__init__(message, request=request, response=response)
        self.excerpt = excerpt
        self._message = message

    def __str__(self) -> str:
        return self._message


# 上游回显的密钥（sk-xxx / Bearer xxx）一律打码
_SECRET_IN_TEXT = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def _sanitize_upstream_text(text: str, limit: int = 300) -> str:
    text = _SECRET_IN_TEXT.sub("***", (text or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text[:limit] + "…" if len(text) > limit else text


def _upstream_error(resp: httpx.Response, raw: str | None = None) -> UpstreamAPIError:
    excerpt = _sanitize_upstream_text(
        resp.text if raw is None else raw)
    message = f"HTTP {resp.status_code} {resp.reason_phrase}".strip()
    if excerpt:
        message += f"（模型服务返回：{excerpt}）"
    return UpstreamAPIError(message, request=resp.request, response=resp,
                            excerpt=excerpt)


def _drop_unsupported_param(body: dict, status: int, raw: str) -> str | None:
    """识别「这个参数我不支持」类 4xx，返回应从 body 里去掉的参数名。

    本地/自建 OpenAI 兼容网关对可选参数的支持参差不齐：没开工具调用的
    vLLM 会因为 ``tools`` 直接 400，模型上下文较小时会因为
    ``max_tokens=8192`` 报 "maximum context length" 400，推理模型会拒绝
    自定义 ``temperature``。遇到这类明确指向某个参数的报错，去掉该参数
    重试一次，比整条链路失败对用户更有用；其余 400（模型名错误、鉴权
    失败等）原样抛出，不做无意义重试。
    """
    if status not in (400, 404, 422):
        return None
    low = (raw or "").lower()
    if "stream_options" in body and "stream_options" in low:
        return "stream_options"
    if "tools" in body and "tool" in low:
        return "tools"
    if "max_tokens" in body and ("max_tokens" in low or "context length" in low
                                 or "maximum context" in low or "too long" in low):
        return "max_tokens"
    if "temperature" in body and "temperature" in low:
        return "temperature"
    return None


def _merge_system_messages(messages: list[dict]) -> list[dict]:
    """把多条 system 合成一条放在开头。

    聊天/会议/作战的上下文会拼进人设、摘要、长期记忆、知识库、技能、
    长度要求、文档能力、工具纪律等多条 system。OpenAI 官方接口接受这种
    写法，但兼容网关普遍只认一条：实测 discovery-api 从第二条 system
    起直接 400 `upstream_request_rejected`（响应体只有 "Bad Request"，
    不指向任何参数），于是每轮对话都失败。合成一条是所有兼容网关都能
    接受的写法，也不改变指令本身的内容与先后顺序。
    """
    parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            text = (m.get("content") or "").strip()
            if text:
                parts.append(text)
        else:
            rest.append(m)
    if len(parts) <= 1:
        return messages
    return [{"role": "system", "content": "\n\n".join(parts)}, *rest]


def _retry_delay(attempt: int, status: int | None = None,
                 retry_after: float | None = None) -> float:
    """指数退避 + jitter；attempt 从 0 开始。

    429 是限流而不是故障：上游通常几秒内就恢复，按 5xx 的 0.5s 起步重试
    基本等于"重试了但必然还失败"（Agnes 的 3.0-flash 实测 2/3 概率 429）。
    因此 429 单独用更长基数，并优先采用上游 `Retry-After`——但封顶
    `RETRY_MAX_DELAY_S`，避免被响应要求睡几分钟卡住整个对话。
    """
    if retry_after is not None and 0 < retry_after <= RETRY_MAX_DELAY_S:
        return retry_after
    if status == 429:
        base = min(RETRY_MAX_DELAY_S, 2.0 * (2 ** attempt))
    else:
        base = min(RETRY_MAX_DELAY_S, RETRY_BASE_DELAY_S * (2 ** attempt))
    return base + random.uniform(0, 0.25)


async def _post_with_retry(url: str, *, json: dict,
                           headers: dict | None = None,
                           timeout: float | httpx.Timeout | None = None) -> httpx.Response:
    """POST 并在可重试错误上做有限退避重试。

    4xx（除 408/425/429）直接抛出，不重试——配置错误/鉴权失败重试再多次
    也不会成功，只会浪费预算。
    ``timeout`` 可覆盖共享 client 的默认分阶段超时（群聊等短回复场景用）。
    """
    client = get_http_client()
    kwargs = {"timeout": timeout} if timeout is not None else {}
    last_exc: Exception | None = None
    for attempt in range(MAX_HTTP_RETRIES + 1):
        try:
            resp = await client.post(url, json=json, headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt >= MAX_HTTP_RETRIES:
                raise
            delay = _retry_delay(attempt)
            logger.warning("模型请求网络异常（%s），%.2fs 后重试 %d/%d",
                           type(exc).__name__, delay,
                           attempt + 1, MAX_HTTP_RETRIES)
            await asyncio.sleep(delay)
            continue
        if _is_retryable_status(resp.status_code) and attempt < MAX_HTTP_RETRIES:
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            delay = _retry_delay(attempt, resp.status_code, retry_after)
            logger.warning("模型返回 HTTP %d，%.2fs 后重试 %d/%d",
                           resp.status_code, delay,
                           attempt + 1, MAX_HTTP_RETRIES)
            await resp.aclose()
            await asyncio.sleep(delay)
            continue
        if resp.status_code >= 400:
            logger.warning("模型请求 %s 返回 HTTP %d：%s", url,
                           resp.status_code, _sanitize_upstream_text(resp.text))
            raise _upstream_error(resp)
        return resp
    raise last_exc or RuntimeError("model request failed without exception")


def is_non_retryable_4xx(exc: Exception) -> bool:
    """是否为"换模型也没用"的 4xx（4xx 不 failover，直接失败）。"""
    code = _http_status(exc)
    return bool(code and 400 <= code < 500 and code not in RETRYABLE_STATUS)

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

    async def stream(self, messages: list[dict], tools=None, config: dict | None = None):
        """流式输出（异步生成器）。

        事件约定：
        * ``{"type": "delta", "text": str}`` — 正文增量，按顺序追加
        * ``{"type": "final", "usage": dict, "tool_calls": list|None, ...}`` — 收尾

        默认实现退化成"整段一次返回"：没实现流式的 provider 仍然能跑流式
        接口，前端表现为整段一起出现，而不是报 500。
        """
        result = await self.chat(messages, tools=tools, config=config)
        content = result.get("content") or ""
        if content:
            yield {"type": "delta", "text": content}
        yield {"type": "final", "usage": result.get("usage") or {},
               "tool_calls": result.get("tool_calls"),
               "raw_model": result.get("raw_model")}


class OpenAICompatProvider(BaseModelProvider):
    """Works for OpenAI / DeepSeek / Qwen / agnes / llama.cpp / any OpenAI-compatible API."""

    provider_type = ModelProviderType.OPENAI_COMPAT

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id

    def _explain_error(self, exc: UpstreamAPIError) -> UpstreamAPIError:
        """把"下一步该做什么"写进错误里。

        上游只会说 "Token not provided" / "Invalid token" / "RateLimitError"，
        不解释是本机没解析到密钥，还是 Key 被拒、账号欠费、还是单纯限流。
        用户只能反复确认"我配置是对的"。
        """
        if exc.response is None:
            return exc
        status = exc.response.status_code
        hint = ""
        if status in (401, 403):
            low = (exc.excerpt or "").lower()
            if "quota" in low or "insufficient" in low or "balance" in low:
                hint = ("（账号额度不足：上游账户余额/配额为 0，请到服务商控制台充值，"
                        "或改用其它模型。这不是 Key 配错）")
            elif not self.api_key:
                hint = ("（本次请求未携带 API Key：请到「模型配置」为这个模型填写 Key，"
                        "或确认对应环境变量已写入 .env 后重启后端）")
            else:
                hint = ("（Key 已发送但被服务商拒绝：可能已失效/被重置，或与站点不匹配"
                        "——Agnes 国际站 .com 与国内站 .cn 的 Key 不通用。"
                        "请到服务商控制台重新生成后，在「模型配置」里更新）")
        elif status == 429:
            hint = ("（被上游限流：该账号/该模型当前并发或频率超限，稍后重试；"
                    "持续出现请换一个模型，或到服务商控制台查看套餐额度）")
        if not hint:
            return exc
        return UpstreamAPIError(str(exc) + hint, request=exc.request,
                                response=exc.response, excerpt=exc.excerpt)

    async def chat(self, messages: list[dict], tools=None, config: dict | None = None) -> dict:
        cfg = config or {}
        body = {
            "model": self.model_id,
            "messages": _merge_system_messages(messages),
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": cfg.get("max_tokens", 8192),
        }
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        # 复用模块级共享 client：连接池有上限、超时分阶段，
        # 且对超时/5xx/429 做有限的指数退避重试。
        url = f"{self.base_url}/chat/completions"
        while True:
            try:
                resp = await _post_with_retry(
                    url, json=body, headers=headers, timeout=cfg.get("timeout"))
                break
            except UpstreamAPIError as exc:
                dropped = _drop_unsupported_param(body, exc.response.status_code,
                                                  exc.excerpt)
                if not dropped:
                    raise self._explain_error(exc)
                body.pop(dropped, None)
                logger.warning("模型 %s 不支持参数 %s，去掉后重试：%s",
                               self.model_id, dropped, exc.excerpt)
        data = resp.json()
        choice = data["choices"][0]
        return {
            "content": choice["message"].get("content") or "",
            "tool_calls": choice["message"].get("tool_calls"),
            "usage": data.get("usage", {}),
            "raw_model": data.get("model", self.model_id),
        }

    async def stream(self, messages, tools=None, config=None):
        """SSE 流式调用。

        与 ``chat`` 的三点差别，都是流式特有的坑：
        1. **重试窗口**：只要还没吐出第一个字，超时/5xx/429 仍可重试；
           一旦开始输出就锁定——重试会让用户看到两段重复的话。
        2. **用量**：``stream_options.include_usage`` 只有部分服务支持，
           遇到 400/404/422 且响应提到 stream_options 时去掉该参数重试，
           否则"想拿用量"会变成"不能用流式"。
        3. **工具调用**：流式的 tool_calls 是分片下发的（id/name 在前，
           arguments 逐段拼接），这里按 index 累积后一次性交给调用方。
        """
        cfg = config or {}
        base_body = {
            "model": self.model_id,
            "messages": _merge_system_messages(messages),
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": cfg.get("max_tokens", 8192),
            "stream": True,
        }
        if tools:
            base_body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        timeout = cfg.get("timeout")
        kwargs = {"timeout": timeout} if timeout is not None else {}

        body = dict(base_body)
        body["stream_options"] = {"include_usage": True}
        last_error: Exception | None = None
        attempt = 0  # 只统计真正失败的重试；参数降级不占用重试次数
        max_attempts = MAX_HTTP_RETRIES + 1
        while attempt < max_attempts:
            emitted = False
            usage: dict = {}
            acc: dict[int, dict] = {}
            try:
                async with get_http_client().stream(
                        "POST", f"{self.base_url}/chat/completions",
                        json=body, headers=headers, **kwargs) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "ignore")
                        dropped = _drop_unsupported_param(body, resp.status_code, raw)
                        if dropped:
                            body.pop(dropped, None)
                            max_attempts += 1
                            logger.info("模型 %s 不支持参数 %s，去掉后重试",
                                        self.model_id, dropped)
                            continue
                        logger.warning("模型 %s 流式请求返回 HTTP %d：%s",
                                       self.model_id, resp.status_code,
                                       _sanitize_upstream_text(raw))
                        raise self._explain_error(_upstream_error(resp, raw))
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            # 推理模型（DeepSeek-R1 / Qwen 等）的思考流单独下发：
                            # reasoning_content 是主流字段，个别网关用 reasoning。
                            thought = (delta.get("reasoning_content")
                                       or delta.get("reasoning"))
                            if thought:
                                # 思考一旦可见就不再 failover：换模型会让同一段
                                # 思考前后来自不同模型，比报错更让人困惑。
                                emitted = True
                                yield {"type": "reasoning", "text": thought}
                            piece = delta.get("content")
                            if piece:
                                emitted = True
                                yield {"type": "delta", "text": piece}
                            for tc in delta.get("tool_calls") or []:
                                slot = acc.setdefault(tc.get("index", 0), {
                                    "id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name") and not slot["function"]["name"]:
                                    slot["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += fn["arguments"]
                yield {"type": "final", "usage": usage,
                       "tool_calls": [acc[k] for k in sorted(acc)] if acc else None,
                       "raw_model": self.model_id}
                return
            except Exception as exc:
                last_error = exc
                if emitted:
                    raise
                attempt += 1
                retryable = (
                    isinstance(exc, (httpx.TimeoutException, httpx.NetworkError,
                                     httpx.RemoteProtocolError))
                    or _is_retryable_status(_http_status(exc) or 0))
                if not retryable or attempt >= max_attempts:
                    raise
                delay = _retry_delay(attempt - 1, _http_status(exc))
                logger.warning("模型流式请求失败（%s），%.2fs 后重试 %d/%d",
                               type(exc).__name__, delay,
                               attempt, max_attempts - 1)
                await asyncio.sleep(delay)
        raise last_error or RuntimeError("model stream failed without exception")


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

# 【重要限制】下面是**进程内**状态：多 worker（uvicorn --workers N / 多机部署）
# 下每个进程各自独立计数与冷却，A 进程熔断不影响 B 进程，重启即清零。
# 这是有意的轻量实现（避免强依赖 Redis）；若需要全局一致的熔断，
# 应把这两个字典迁移到 Redis/DB，而不是假设当前实现是全局的。
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


# 模型行里可以下发给 provider 的调用参数。只认这三个：其余 config 项
# （尺寸、时长、上游元数据）属于生成类接口的配置，不能混进 chat 请求体。
_RUNTIME_CONFIG_KEYS = ("temperature", "max_tokens", "timeout")


def _runtime_config(model_row: Model, config: dict | None) -> dict | None:
    """调用方显式 config 优先；没给的键才用模型行里保存的默认值。

    这样「模型配置中心」设置的 temperature / max_tokens 才真正生效，
    同时不会覆盖上层（如 CEO 对话）按场景传入的参数。
    """
    stored = model_row.config if isinstance(model_row.config, dict) else {}
    merged = dict(config or {})
    for key in _RUNTIME_CONFIG_KEYS:
        if key not in merged and stored.get(key) is not None:
            merged[key] = stored[key]
    return merged or None


def _metric_labels(model_row: Model) -> tuple[str, str]:
    """LLM 指标的标签：provider + 模型名（低基数，不用每次请求的 id）。"""
    raw_provider = getattr(model_row, "provider_type", "")
    provider = str(getattr(raw_provider, "value", raw_provider) or "unknown")
    provider = provider.rsplit(".", 1)[-1][:40] or "unknown"
    model = str(getattr(model_row, "model_id", None)
                or getattr(model_row, "name", None) or "unknown")[:100]
    return provider, model or "unknown"


def _observe_llm_call(model_row: Model, outcome: str, duration_s: float,
                      usage: dict | None = None) -> None:
    """记录一次 LLM 调用（成功/失败都记）。

    指标本身绝不能影响调用语义：这里只捕获"指标计算/写入"的异常并留日志，
    不捕获、不吞掉调用方的业务异常。
    """
    try:
        provider, model = _metric_labels(model_row)
        LLM_REQUESTS_TOTAL.inc(provider=provider, model=model, outcome=outcome)
        LLM_DURATION_SECONDS.observe(
            max(0.0, float(duration_s)), provider=provider, model=model)
        payload = usage or {}
        # OpenAI 兼容接口用 prompt/completion_tokens；部分自建网关用
        # input/output_tokens。两种都认，拿不到就记 0（不凭空造数）。
        input_tokens = int(payload.get("prompt_tokens")
                           or payload.get("input_tokens") or 0)
        output_tokens = int(payload.get("completion_tokens")
                            or payload.get("output_tokens") or 0)
        if input_tokens > 0:
            LLM_TOKENS_TOTAL.inc(input_tokens, provider=provider,
                                 model=model, kind="input")
        if output_tokens > 0:
            LLM_TOKENS_TOTAL.inc(output_tokens, provider=provider,
                                 model=model, kind="output")
    except Exception as metric_err:  # 指标失败不影响模型调用
        logger.warning("LLM 指标记录失败 model=%s: %s",
                       getattr(model_row, "name", "?"), metric_err)


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
        # 日预算窗口按 UTC 日起算：TokenUsage.created_at 列是 naive DateTime，
        # 默认值写入的正是 UTC 时间（models/token_usage._now）。此前用本地时区
        # 的当日 0 点比较，东八区下窗口错位 8 小时。这里保持 naive-UTC，
        # 与库中存储格式一致，避免 naive/aware 比较异常。
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        for b in budgets:
            if b.scope_type == "agent" and b.scope_id != ctx.get("agent_id"):
                continue
            # V5.5 A4：部门级虚拟账户（scope_id=部门 id）
            if b.scope_type == "department":
                dep = ctx.get("department_id") or (
                    str(getattr(agent_obj, "department_id", None))
                    if getattr(agent_obj, "department_id", None) else None)
                if b.scope_id != dep:
                    continue
            q = select(
                func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
            ).where(TokenUsage.company_id == company_id)
            if b.scope_type == "agent" and b.scope_id:
                q = q.where(TokenUsage.agent_id == b.scope_id)
            elif b.scope_type == "department" and b.scope_id:
                q = q.where(TokenUsage.department_id == b.scope_id)
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
                # V5.6.1 tripped 语义（学 OpenAI Agents SDK）：结构化熔断原因
                # ——暂停≠失败，老板可加预算原状态续跑
                import json as _json
                b.paused_reason = _json.dumps({
                    "tripped": True,
                    "cause": "budget",
                    "period": b.period,
                    "tokens_used": tokens, "tokens_limit": b.limit_tokens,
                    "cost_used": round(cost, 4), "cost_limit": b.limit_cost,
                    "message": (f"用量到顶（{tokens}/"
                                f"{'∞' if not b.limit_tokens else b.limit_tokens}"
                                f" token, ¥{cost:.2f}/"
                                f"{'∞' if not b.limit_cost else b.limit_cost:.2f}），已自动暂停"),
                }, ensure_ascii=False)
                # 关键修复：熔断状态必须落库，但不能在调用方的业务事务中间
                # commit（那会把"任务已转 RUNNING"等半成品状态提前提交，
                # 后续失败无法整体回滚）。这里用独立 session 只写这一行。
                try:
                    async with AsyncSessionLocal() as budget_db:
                        await budget_db.execute(
                            update(TokenBudget)
                            .where(TokenBudget.id == b.id)
                            .values(paused=True, paused_reason=b.paused_reason))
                        await budget_db.commit()
                except Exception as commit_err:
                    # 暂停状态落库失败会导致预算"到顶不停"的账目缺口，
                    # 属于成本护栏失效，必须留痕（但仍不阻断本次 BudgetExhausted 上抛）
                    logger.error("预算暂停状态落库失败 budget=%s: %s",
                                 getattr(b, "id", "?"), commit_err, exc_info=True)
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
            if row.enabled is False:
                failures.append(f"{row.name}：已在模型配置中停用")
                logger.info("模型 %s(%s) 已停用，跳过本次调用", row.name, mid)
                continue
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
            metric_t0 = time.perf_counter()
            try:
                result = await provider.chat(
                    messages, tools=tools,
                    config=_runtime_config(row, config))
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
                _observe_llm_call(row, "success",
                                  time.perf_counter() - metric_t0,
                                  result.get("usage"))
                return result
            except NotImplementedError as e:
                last_error = e
                err_summary = f"未实现：{e}"
                failures.append(f"{row.name}：{err_summary}")
                logger.warning("模型 %s(%s) 调用失败：%s", row.name, mid, err_summary)
                _note_failure(mid)
                _observe_llm_call(row, "error",
                                  time.perf_counter() - metric_t0)
                continue
            except Exception as e:  # failover to next model
                last_error = e
                err_summary = f"{type(e).__name__}: {e}"
                failures.append(f"{row.name}：{err_summary}")
                logger.warning("模型 %s(%s) 调用失败：%s",
                               row.name, mid, err_summary)
                _note_failure(mid)
                _observe_llm_call(row, "error",
                                  time.perf_counter() - metric_t0)
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
                # 4xx（鉴权/参数/模型不存在等）换模型也没用：
                # 直接失败，不再把同一份坏请求扩散到备用模型。
                if is_non_retryable_4xx(e):
                    logger.warning("模型 %s(%s) 返回不可重试的 4xx，"
                                   "直接失败不 failover", row.name, mid)
                    break
                # 429（重试后仍限流）同样短路：继续打备用模型只会扩大限流
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

    async def stream_with_agent_models(
        self,
        model_ids: list[str],
        messages: list[dict],
        tools=None,
        config: dict | None = None,
    ):
        """流式版 ``chat_with_agent_models``：事件化输出 + 首字前可 failover。

        事件：
        * ``{"type": "delta", "text": ...}`` 正文增量（直接转发给前端）
        * ``{"type": "done", "usage", "tool_calls", "model", "used_model_id"}`` 收尾

        **一旦吐过字就不再换模型**：换模型重来会让用户看到两段不同模型的话
        拼在一起，比直接报错更糟。代价是流式中途故障无法静默兜底。
        """
        if not model_ids:
            raise RuntimeError(
                "该员工尚未绑定任何模型，无法执行任务。"
                "请到「模型配置」登记一个模型并配置密钥，"
                "再在员工详情页把它绑定为首选模型。")

        ctx = usage_ctx.get()
        await self._enforce_budget(ctx)
        attempted = 0
        missing: list[str] = []
        failures: list[str] = []
        for mid in model_ids:
            row = await self.db.get(Model, mid)
            if row is None:
                missing.append(mid)
                continue
            attempted += 1
            if row.enabled is False:
                failures.append(f"{row.name}：已在模型配置中停用")
                logger.info("模型 %s(%s) 已停用，跳过本次流式调用", row.name, mid)
                yield {"type": "model_error", "model_id": mid,
                       "model_name": row.name, "message": "已在模型配置中停用"}
                continue
            if _in_cooldown(mid):
                failures.append(f"{row.name}：熔断冷却中，已跳过")
                logger.warning("模型 %s(%s) 处于熔断冷却期，跳过本次流式调用",
                               row.name, mid)
                yield {"type": "model_error", "model_id": mid,
                       "model_name": row.name, "message": "熔断冷却中，已跳过"}
                continue
            provider = self._build_provider(row)
            if provider is None:
                failures.append(f"{row.name}：未知 provider 类型 {row.provider_type}")
                yield {"type": "model_error", "model_id": mid,
                       "model_name": row.name,
                       "message": f"未知 provider 类型 {row.provider_type}"}
                continue
            # 先报"用哪个模型"，前端才能把长时间的等待归因到具体模型，
            # 也能在降级时看见换了谁。
            yield {"type": "model", "model_id": mid,
                   "model_name": row.name, "attempt": attempted}
            t0 = time.time()
            metric_t0 = time.perf_counter()
            emitted = False
            final: dict = {}
            try:
                async for ev in provider.stream(
                        messages, tools=tools,
                        config=_runtime_config(row, config)):
                    if ev.get("type") == "delta":
                        emitted = True
                        yield ev
                    elif ev.get("type") == "final":
                        final = ev
            except Exception as e:
                if emitted:
                    # 已经推给用户了：不能重放，直接上抛让调用方报错
                    logger.error("模型 %s(%s) 流式中途失败，已输出部分内容不重试：%s",
                                 row.name, mid, e)
                    _observe_llm_call(row, "error",
                                      time.perf_counter() - metric_t0)
                    raise
                last_summary = f"{type(e).__name__}: {e}"
                failures.append(f"{row.name}：{last_summary}")
                logger.warning("模型 %s(%s) 流式调用失败：%s",
                               row.name, mid, last_summary)
                yield {"type": "model_error", "model_id": mid,
                       "model_name": row.name, "message": last_summary}
                _note_failure(mid)
                _observe_llm_call(row, "error",
                                  time.perf_counter() - metric_t0)
                try:
                    await record_token_usage(
                        self.db, {}, model_row=row, ctx=ctx,
                        latency_ms=int((time.time() - t0) * 1000),
                        success=False)
                except Exception as usage_err:
                    logger.error("失败调用计量落库失败 model=%s mid=%s: %s",
                                 row.name, mid, usage_err, exc_info=True)
                if is_non_retryable_4xx(e) or _http_status(e) == 429:
                    break
                continue

            _note_success(mid)
            try:
                await record_token_usage(
                    self.db, {"usage": final.get("usage") or {}},
                    model_row=row, ctx=ctx,
                    latency_ms=int((time.time() - t0) * 1000))
            except Exception as usage_err:
                logger.error("流式调用计量落库失败 model=%s mid=%s: %s",
                             row.name, mid, usage_err, exc_info=True)
            _observe_llm_call(row, "success",
                              time.perf_counter() - metric_t0,
                              final.get("usage"))
            yield {"type": "done", "usage": final.get("usage") or {},
                   "tool_calls": final.get("tool_calls"),
                   "model": row.name, "used_model_id": mid,
                   "elapsed_ms": int((time.time() - t0) * 1000)}
            return

        if attempted == 0:
            raise RuntimeError(
                f"该员工绑定的模型已不存在（可能已被删除）：{', '.join(missing[:3])}。"
                "请重新绑定一个可用模型。")
        raise RuntimeError(
            f"已绑定的 {attempted} 个模型全部调用失败：{'; '.join(failures)}")

    async def chat_with_agent_models_by_agent(self, agent, messages,
                                              tools=None, config=None):
        """按 Agent 的模型配置（primary→secondary→fallback）调用。

        ``config`` 透传给 provider（max_tokens / temperature / timeout），
        群聊等短回复场景用它显式限长并缩短单次超时。
        """
        from sqlalchemy import select
        from app.models.agent import AgentModelConfig
        cfg_rows = (await self.db.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == str(agent.id)))).scalars().all()
        order = {"primary": 0, "secondary": 1, "fallback": 2}
        model_ids = [c.model_id for c in sorted(cfg_rows,
                    key=lambda c: order.get(c.role, 3))]
        if not model_ids:
            raise RuntimeError(f"Agent {agent.name} 未配置模型")
        return await self.chat_with_agent_models(
            model_ids, messages, tools=tools, config=config)
