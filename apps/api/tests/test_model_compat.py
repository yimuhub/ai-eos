"""模型网关：上游错误可见性 + 兼容降级。

背景：用户接自建 OpenAI 兼容网关时收到
``HTTPStatusError: Client error '400 Bad Request'``，拿不到任何可操作信息。
这里锁定两条行为：

1. 上游响应体（脱敏、截断）必须进异常消息；
2. 「某个可选参数不被支持」类 400（tools / max_tokens / stream_options）
   自动去掉该参数重试，而不是让整条链路失败。
"""
import json

import httpx
import pytest

from app import model_gateway


def _use_transport(monkeypatch, handler):
    """把网关的共享 client 换成 MockTransport 驱动的 client。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(model_gateway, "get_http_client", lambda: client)
    return client


def _completion(content: str = "好的") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "Sage-A1.5",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def _sse(text: str = "你好") -> httpx.Response:
    chunks = [
        {"model": "Sage-A1.5",
         "choices": [{"delta": {"content": text}}]},
        {"model": "Sage-A1.5", "choices": [],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=body.encode())


async def test_chat_error_message_contains_redacted_upstream_body(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {
            "message": "model 'Sage-A1.5' does not exist; key=sk-abcdefgh12345678"}})

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://192.168.3.44:8787/v1", "sk-abcdefgh12345678", "Sage-A1.5")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        await provider.chat([{"role": "user", "content": "你好"}])

    message = str(exc_info.value)
    assert "400" in message
    assert "does not exist" in message          # 上游原因可见
    assert "sk-abcdefgh12345678" not in message  # 密钥不泄露
    assert "***" in message
    assert len(seen) == 1                        # 与参数无关的 400 不瞎重试


async def test_chat_drops_tools_when_server_rejects_them(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {
                "message": "tools are not supported by this model"}})
        return _completion("没有工具也能回答")

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")
    tools = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]

    result = await provider.chat([{"role": "user", "content": "查一下"}], tools=tools)

    assert result["content"] == "没有工具也能回答"
    assert len(bodies) == 2
    assert "tools" in bodies[0]
    assert "tools" not in bodies[1]
    assert bodies[1]["messages"] == bodies[0]["messages"]  # 只去掉参数，不改请求语义


_MULTI_SYSTEM_MSGS = [
    {"role": "system", "content": "你是「战略研究员」。"},
    {"role": "system", "content": "你的长期记忆：\n- 上月营收 12 万"},
    {"role": "user", "content": "上一轮的问题"},
    {"role": "assistant", "content": "上一轮的回答"},
    {"role": "system", "content": "回答长度要求：300-800 字。"},
    {"role": "system", "content": "【文档产出能力】……"},
    {"role": "user", "content": "你好，请用一句话介绍你自己"},
]


async def test_chat_sends_a_single_system_message(monkeypatch):
    """兼容网关只认一条 system：多条会 400 upstream_request_rejected。

    真实场景：聊天上下文拼了人设/记忆/知识库/技能等 6~9 条 system，
    discovery-api 从第二条起直接拒绝，单聊每轮都回「（没有收到内容）」。
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if sum(1 for m in body["messages"] if m["role"] == "system") > 1:
            return httpx.Response(400, json={"error": {
                "code": "upstream_request_rejected", "message": "Bad Request",
                "type": "invalid_request_error"}})
        return _completion("我是战略研究员")

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    result = await provider.chat(_MULTI_SYSTEM_MSGS)

    assert result["content"] == "我是战略研究员"
    sent = bodies[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    merged = sent[0]["content"]
    # 每条 system 原文都在，且先后顺序不变
    assert "你是「战略研究员」。" in merged
    assert "【文档产出能力】……" in merged
    assert merged.index("你是「战略研究员」。") < merged.index("回答长度要求")
    # 对话消息原样保留
    assert sent[3] == {"role": "user", "content": "你好，请用一句话介绍你自己"}


async def test_stream_sends_a_single_system_message(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if sum(1 for m in body["messages"] if m["role"] == "system") > 1:
            return httpx.Response(400, json={"error": {
                "code": "upstream_request_rejected", "message": "Bad Request",
                "type": "invalid_request_error"}})
        return _sse("你好")

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    events = [ev async for ev in provider.stream(_MULTI_SYSTEM_MSGS)]

    assert [ev["type"] for ev in events if ev["type"] == "delta"] == ["delta"]
    assert sum(1 for m in bodies[0]["messages"] if m["role"] == "system") == 1


async def test_single_system_message_is_left_untouched(monkeypatch):
    """只有一条 system 时不重排消息：避免给正常网关改请求体。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _completion()

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")
    messages = [{"role": "system", "content": "人设"},
                {"role": "user", "content": "你好"}]

    await provider.chat(messages)

    assert bodies[0]["messages"] == messages


async def test_chat_drops_max_tokens_on_context_length_error(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {
                "message": "This model's maximum context length is 2048 tokens. "
                           "However, you requested 8192 tokens."}})
        return _completion()

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    result = await provider.chat([{"role": "user", "content": "你好"}],
                                 config={"max_tokens": 8192})

    assert result["content"] == "好的"
    assert "max_tokens" in bodies[0]
    assert "max_tokens" not in bodies[1]


async def test_chat_drops_temperature_when_rejected(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'temperature' is not supported "
                           "with this model."}})
        return _completion("推理模型也能答")

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    result = await provider.chat([{"role": "user", "content": "你好"}])

    assert result["content"] == "推理模型也能答"
    assert "temperature" in bodies[0]
    assert "temperature" not in bodies[1]


async def test_missing_key_401_points_to_model_config(monkeypatch):
    """没带密钥导致 401 时，错误里必须告诉用户去补 Key。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in request.headers}
        return httpx.Response(401, json={"error": {
            "message": "Token not provided", "type": "AgnesAI_error"}})

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "https://apihub.agnes-ai.com/v1", "", "agnes-3.0-flash")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        await provider.chat([{"role": "user", "content": "你好"}])

    message = str(exc_info.value)
    assert "Token not provided" in message
    assert "未携带 API Key" in message


async def test_401_with_key_does_not_claim_key_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "https://apihub.agnes-ai.com/v1", "sk-configured-key", "agnes-3.0-flash")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        await provider.chat([{"role": "user", "content": "你好"}])

    assert "invalid api key" in str(exc_info.value)
    assert "未携带 API Key" not in str(exc_info.value)
    assert "被服务商拒绝" in str(exc_info.value)   # 指向"Key 失效/站点不匹配"


async def test_stream_surfaces_upstream_error_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request payload"}})

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        async for _ in provider.stream([{"role": "user", "content": "你好"}]):
            pass

    assert "bad request payload" in str(exc_info.value)


async def test_429_explains_rate_limit_instead_of_raw_status(monkeypatch):
    """429 不是"Key 错了"。必须提示限流/额度，否则用户会去反复改配置。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {
            "message": "RateLimitError: OpenAIException -",
            "type": "throttling_error", "code": "429"}})

    _use_transport(monkeypatch, handler)
    monkeypatch.setattr(model_gateway, "_retry_delay", lambda *a, **k: 0)
    provider = model_gateway.OpenAICompatProvider(
        "https://apihub.agnes-ai.com/v1", "sk-configured-key", "agnes-3.0-flash")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        await provider.chat([{"role": "user", "content": "你好"}])

    message = str(exc_info.value)
    assert "被上游限流" in message
    assert "被服务商拒绝" not in message


def test_429_backoff_is_longer_than_5xx_and_capped(monkeypatch):
    """429 要等得起（2s 起步），又不能被 Retry-After 拖到几分钟。"""
    monkeypatch.setattr(model_gateway.random, "uniform", lambda a, b: 0.0)

    assert model_gateway._retry_delay(0, 500) == 0.5
    assert model_gateway._retry_delay(0, 429) == 2.0
    assert model_gateway._retry_delay(1, 429) == 4.0
    assert model_gateway._retry_delay(0, 429, retry_after=1.5) == 1.5
    # 超过封顶就忽略 Retry-After，按自己的 429 退避来
    assert model_gateway._retry_delay(0, 429, retry_after=120) == 2.0


async def test_403_quota_points_to_balance_not_key(monkeypatch):
    """账户余额为 0 的 403，不能误导成"Key 被拒"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {
            "message": "Insufficient user quota, remaining: $0.000000",
            "type": "AgnesAI_error"}})

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "https://apihub.agnes-ai.com/v1", "sk-configured-key", "agnes-2.5-pro-alpha")

    with pytest.raises(model_gateway.UpstreamAPIError) as exc_info:
        await provider.chat([{"role": "user", "content": "你好"}])

    message = str(exc_info.value)
    assert "额度不足" in message
    assert "被服务商拒绝" not in message


async def test_stream_drops_stream_options_then_succeeds(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream_options"):
            return httpx.Response(400, json={"error": {
                "message": "stream_options is not supported"}})
        return _sse("流式可以")

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    deltas: list[str] = []
    async for ev in provider.stream([{"role": "user", "content": "你好"}]):
        if ev["type"] == "delta":
            deltas.append(ev["text"])

    assert deltas == ["流式可以"]
    assert len(bodies) == 2
    assert bodies[0].get("stream_options")
    assert "stream_options" not in bodies[1]


# ---------------------------------------------------------------------------
# 流式过程可见性：思考增量 + 模型选择/降级事件
# ---------------------------------------------------------------------------
async def test_stream_forwards_reasoning_deltas(monkeypatch):
    """推理模型的 reasoning_content / reasoning 要单独下发，不能丢。"""

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "先核对数据"}}]},
            {"choices": [{"delta": {"reasoning": "再给结论"}}]},
            {"choices": [{"delta": {"content": "结论如下"}}]},
            {"choices": [], "usage": {"total_tokens": 9}},
        ]
        body = "".join(
            f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
        body += "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode())

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "R1")

    events = [ev async for ev in provider.stream(
        [{"role": "user", "content": "你好"}])]

    assert [ev["text"] for ev in events if ev["type"] == "reasoning"] == [
        "先核对数据", "再给结论"]
    assert [ev["text"] for ev in events if ev["type"] == "delta"] == ["结论如下"]


async def test_stream_with_agent_models_announces_fallback(monkeypatch):
    """首字之前可以降级；前端要能看见"用了谁、谁失败了"。"""
    from types import SimpleNamespace

    good = SimpleNamespace(id="m-good", name="好模型", provider_type="openai_compat",
                           base_url="", model_id="good", api_key="",
                           enabled=None, config=None)
    bad = SimpleNamespace(id="m-bad", name="坏模型", provider_type="openai_compat",
                          base_url="", model_id="bad", api_key="",
                          enabled=None, config=None)

    class Db:
        async def get(self, model, mid):
            return {"m-good": good, "m-bad": bad}.get(mid)

    class BadProvider:
        async def stream(self, messages, tools=None, config=None):
            raise RuntimeError("upstream boom")
            yield  # pragma: no cover

    class GoodProvider:
        async def stream(self, messages, tools=None, config=None):
            yield {"type": "delta", "text": "兜底成功"}
            yield {"type": "final", "usage": {}, "tool_calls": None}

    providers = {"m-bad": BadProvider(), "m-good": GoodProvider()}
    monkeypatch.setattr(model_gateway.ModelGateway, "_build_provider",
                        staticmethod(lambda row: providers[row.id]))

    async def _noop_usage(*args, **kwargs):
        return None

    monkeypatch.setattr(model_gateway, "record_token_usage", _noop_usage)
    monkeypatch.setattr(model_gateway, "_observe_llm_call",
                        lambda *a, **k: None)

    events = [ev async for ev in model_gateway.ModelGateway(
        Db()).stream_with_agent_models(
            ["m-bad", "m-good"], [{"role": "user", "content": "hi"}])]

    kinds = [ev["type"] for ev in events]
    assert kinds == ["model", "model_error", "model", "delta", "done"]
    assert events[0]["model_name"] == "坏模型"
    assert events[1]["message"].startswith("RuntimeError")
    assert events[2]["model_name"] == "好模型" and events[2]["attempt"] == 2
    assert events[4]["used_model_id"] == "m-good"
    assert isinstance(events[4]["elapsed_ms"], int)


async def test_chat_surfaces_finish_reason(monkeypatch):
    """空正文要能看出"是被截断的还是采样踩空"，否则重试策略只能靠猜。

    真实场景：推理型模型的 max_tokens 把"思考 + 正文"一起算，Sage-A1.5 实测
    512 全花在思考上（reasoning 1900+ 字），content 是空但 HTTP 200。
    群聊发言据此决定原样重发还是抬预算重发。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "Sage-A1.5",
            "choices": [{"message": {"role": "assistant", "content": None},
                         "finish_reason": "length"}],
            "usage": {"completion_tokens": 512},
        })

    _use_transport(monkeypatch, handler)
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    result = await provider.chat([{"role": "user", "content": "讲一下"}],
                                 config={"max_tokens": 512})

    assert result["content"] == ""
    assert result["finish_reason"] == "length"


async def test_chat_finish_reason_is_none_when_upstream_omits_it(monkeypatch):
    """不规范的老网关不带 finish_reason，取空值而不是 KeyError。"""
    _use_transport(monkeypatch, lambda request: _completion("好的"))
    provider = model_gateway.OpenAICompatProvider(
        "http://127.0.0.1:8787/v1", "", "Sage-A1.5")

    result = await provider.chat([{"role": "user", "content": "在吗"}])

    assert result["finish_reason"] is None
