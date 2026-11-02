"""上游模型发现 + 类型识别 + 按类型生成默认配置。

供应商只提供「地址 + 密钥」，这一层负责把它变成可选择的模型清单：

* 兼容三种列表协议：OpenAI 兼容 `/v1/models`、Anthropic `/v1/models`
  （x-api-key）、Gemini `/v1beta/models`（x-goog-api-key）；OpenAI 兼容
  地址拉取失败时再尝试 Ollama 原生 `/api/tags`。
* 类型判断优先用上游元数据（input/output modalities），拿不到再按模型名
  的稳定特征兜底，最后默认文本。
* 每个模型都带一份「按类型系统化」的默认配置；上游给了上下文长度等
  参数就合并进去，前端只需在缺失或想覆盖时手改。

安全边界与 `models_api` 一致：base_url 过出站闸门（允许私网本地模型、
拒绝云元数据地址），不跟随重定向，错误不回显上游响应体，也不记录密钥。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.models.agent import ModelProviderType, ModelType

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT = httpx.Timeout(connect=6.0, read=15.0, write=10.0, pool=6.0)

DEFAULT_BASE_URLS = {
    ModelProviderType.OPENAI_COMPAT: "",
    ModelProviderType.ANTHROPIC: "https://api.anthropic.com",
    ModelProviderType.GEMINI: "https://generativelanguage.googleapis.com",
}

_MODEL_TYPE_LABELS = {
    ModelType.TEXT.value: "文本",
    ModelType.IMAGE.value: "图片",
    ModelType.VIDEO.value: "视频",
    ModelType.AUDIO.value: "音频",
    ModelType.EMBEDDING.value: "向量",
    ModelType.OTHER.value: "其他",
}

_VIDEO_HINTS = (
    "sora", "veo", "runway", "kling", "pika", "seedance", "hailuo",
    "cogvideo", "wan2", "wanx-video", "minimax-video", "luma", "ray-2",
    "video", "t2v", "i2v",
)
_IMAGE_HINTS = (
    "dall-e", "dalle", "gpt-image", "gpt-image-1", "image", "flux",
    "stable-diffusion", "sdxl", "imagen", "seedream", "kolors", "recraft",
    "midjourney", "cogview", "qwen-image", "wanx", "ideogram",
)
_AUDIO_HINTS = (
    "whisper", "tts", "audio", "speech", "voice", "music", "suno",
    "elevenlabs", "transcribe", "realtime",
)
_EMBED_HINTS = (
    "embed", "embedding", "bge-", "text-embedding", "voyage", "jina-embed",
    "gte-",
)
_VISION_HINTS = (
    "vision", "vl", "4o", "4.1", "omni", "llava", "pixtral", "gemini",
    "claude-3", "claude-4", "claude-sonnet", "claude-opus", "gpt-5",
    "o3", "o4",
)
_TOOL_HINTS = (
    "gpt-4", "gpt-5", "gpt-41", "o1", "o3", "o4", "claude", "gemini",
    "qwen", "llama-3", "llama-4", "mistral", "deepseek", "glm", "kimi",
    "moonshot", "grok",
)
_REASONING_HINTS = (
    "o1", "o3", "o4", "reason", "thinking", "r1", "qwq", "deepseek-r",
)


@dataclass
class DiscoveryResult:
    """一次拉取的结果。失败时 models 为空、error 是可直接展示的中文原因。"""

    ok: bool
    models: list[dict] = field(default_factory=list)
    error: str | None = None
    base_url: str | None = None


def type_label(model_type: str | None) -> str:
    return _MODEL_TYPE_LABELS.get(model_type or ModelType.TEXT.value, "其他")


# 只有文本模型能当员工的对话模型。图片/视频/音频/向量模型走的是别的接口
# （如 /v1/images/generations），拿它对话必然 400，而上游的报错往往只有一句
# Bad Request，看不出是绑错类型——所以要在绑定入口就拦住。
CHAT_CAPABLE_TYPES = (ModelType.TEXT.value,)


def is_chat_model(model_type: str | None) -> bool:
    """该模型类型能否用于对话。NULL 等价 text（升级前登记的只有聊天模型）。"""
    return model_type is None or model_type in CHAT_CAPABLE_TYPES


def chat_model_condition():
    """`is_chat_model` 的 SQL 版，给"自动挑一个模型"的查询用。"""
    from sqlalchemy import or_

    from app.models.agent import Model
    return or_(Model.model_type.is_(None),
               Model.model_type.in_(CHAT_CAPABLE_TYPES))


def chat_model_rejection(model) -> str:
    """绑定被拒时给前端的中文原因，点名类型而不是丢一个上游 400。"""
    return (f"「{model.name}」是{type_label(model.model_type)}模型，"
            f"不能绑定为对话模型——请改绑文本模型。")


def _norm_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [p.strip().lower() for p in re.split(r"[+,]", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return []


def _modalities(item: dict) -> tuple[set[str], set[str]]:
    """从 OpenAI/OpenRouter 风格元数据中解析输入、输出模态。"""
    arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    modality = str(arch.get("modality") or item.get("modality") or "").lower()
    inputs = set(_norm_list(arch.get("input_modalities")
                            or item.get("input_modalities")))
    outputs = set(_norm_list(arch.get("output_modalities")
                             or item.get("output_modalities")))
    if "->" in modality:
        left, right = modality.split("->", 1)
        inputs |= set(_norm_list(left))
        outputs |= set(_norm_list(right))
    return inputs, outputs


def _upstream_context(item: dict) -> dict:
    """提取上游可能给出的上下文 / 输出上限。字段名各站不统一，这里一并认。"""
    top = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
    context = (item.get("context_length") or item.get("context_window")
               or item.get("max_context_length") or item.get("inputTokenLimit")
               or item.get("max_model_len"))
    output = (item.get("max_completion_tokens") or item.get("max_output_tokens")
              or item.get("outputTokenLimit") or top.get("max_completion_tokens"))
    return {
        "context_window": int(context) if isinstance(context, (int, float)) else None,
        "max_output_tokens": int(output) if isinstance(output, (int, float)) else None,
    }


def _has(name: str, hints: tuple[str, ...]) -> bool:
    return any(h in name for h in hints)


def _gemini_methods(item: dict) -> set[str]:
    return {str(m).lower() for m in item.get("supportedGenerationMethods") or []}


def classify_model(model_id: str, meta: dict | None = None,
                   provider_type: ModelProviderType | str | None = None) -> dict:
    """识别模型类型、多模态与能力标签。

    判定顺序：上游输出的非文本模态 > 上游输入模态 > 模型名特征 > 文本。
    输出优先，因为「能生成图片的聊天模型」在系统里应归为图片模型。
    """
    item = meta or {}
    name = model_id.lower()
    inputs, outputs = _modalities(item)
    methods = _gemini_methods(item)

    if "video" in outputs:
        mtype = ModelType.VIDEO.value
    elif "image" in outputs:
        mtype = ModelType.IMAGE.value
    elif "audio" in outputs:
        mtype = ModelType.AUDIO.value
    elif "embedcontent" in methods or "embedding" in outputs:
        mtype = ModelType.EMBEDDING.value
    elif _has(name, _EMBED_HINTS):
        mtype = ModelType.EMBEDDING.value
    elif _has(name, _VIDEO_HINTS):
        mtype = ModelType.VIDEO.value
    elif _has(name, _IMAGE_HINTS):
        mtype = ModelType.IMAGE.value
    elif _has(name, _AUDIO_HINTS):
        mtype = ModelType.AUDIO.value
    else:
        mtype = ModelType.TEXT.value

    non_text_io = bool(inputs - {"text"} or outputs - {"text"})
    multimodal = bool(
        non_text_io
        or mtype in {ModelType.IMAGE.value, ModelType.VIDEO.value}
        or (mtype == ModelType.TEXT.value and _has(name, _VISION_HINTS))
    )
    if mtype == ModelType.EMBEDDING.value:
        multimodal = False

    capabilities: list[str] = []
    if mtype == ModelType.TEXT.value:
        capabilities = ["chat", "streaming"]
        if multimodal or "image" in inputs:
            capabilities.insert(1, "vision")
        if _has(name, _TOOL_HINTS):
            capabilities.append("tools")
        if _has(name, _REASONING_HINTS):
            capabilities.append("reasoning")
    elif mtype == ModelType.IMAGE.value:
        capabilities = ["image_generation"]
        if "edit" in name:
            capabilities.append("image_edit")
    elif mtype == ModelType.VIDEO.value:
        capabilities = ["video_generation"]
    elif mtype == ModelType.AUDIO.value:
        capabilities = ["audio"]
        if "tts" in name or "speech" in name:
            capabilities.append("text_to_speech")
        if "whisper" in name or "transcri" in name:
            capabilities.append("transcription")
    elif mtype == ModelType.EMBEDDING.value:
        capabilities = ["embeddings"]

    ctx = _upstream_context(item)
    config = default_config_for(mtype, ctx, model_id)
    return {
        "id": model_id,
        "name": str(item.get("name") or item.get("displayName") or model_id),
        "model_type": mtype,
        "type_label": type_label(mtype),
        "multimodal": multimodal,
        "capabilities": capabilities,
        "config": config,
        "upstream": {
            k: item[k] for k in
            ("id", "owned_by", "architecture", "modality", "pricing",
             "context_length", "top_provider", "description",
             "inputTokenLimit", "outputTokenLimit",
             "supportedGenerationMethods")
            if k in item and item[k] is not None
        },
    }


def default_config_for(model_type: str, upstream: dict | None = None,
                       model_id: str = "") -> dict:
    """按模型类型生成系统化默认配置。

    `upstream` 来自 `_upstream_context`；有值就用上游值，没有就落到安全的
    通用默认。前端会把它展示成可编辑表单，而不是一堆看不见的隐式行为。
    """
    ctx = upstream or {}
    config: dict = {}
    context_window = ctx.get("context_window")
    max_output = ctx.get("max_output_tokens")

    if model_type == ModelType.TEXT.value:
        config.update({
            "temperature": 0.7,
            "max_tokens": min(max_output or 4096, 8192),
            "supports_streaming": True,
        })
        if context_window:
            config["context_window"] = context_window
        if max_output:
            config["max_output_tokens"] = max_output
    elif model_type == ModelType.IMAGE.value:
        config.update({
            "default_size": "1024x1024",
            "sizes": ["1024x1024", "1536x1024", "1024x1536"],
            "quality": "standard",
            "response_format": "url",
            "max_images": 4,
        })
    elif model_type == ModelType.VIDEO.value:
        config.update({
            "default_duration": 5,
            "durations": [5, 10],
            "resolutions": ["720p", "1080p"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "fps": 24,
        })
    elif model_type == ModelType.AUDIO.value:
        config.update({
            "default_format": "mp3",
            "formats": ["mp3", "wav", "opus"],
            "default_voice": "",
            "voices": [],
        })
    elif model_type == ModelType.EMBEDDING.value:
        config.update({"batch_size": 64, "dimensions": None})
    return config


def auth_headers(provider_type: ModelProviderType, api_key: str,
                 extra: dict | None = None) -> dict[str, str]:
    """按 provider 生成鉴权头。Anthropic 用 x-api-key，Gemini 用 x-goog-api-key。"""
    headers: dict[str, str] = {}
    if api_key:
        if provider_type is ModelProviderType.ANTHROPIC:
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        elif provider_type is ModelProviderType.GEMINI:
            headers = {"x-goog-api-key": api_key}
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
    if extra:
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, str) and k.lower() not in {
                    "authorization", "x-api-key", "x-goog-api-key", "host"}:
                headers[k] = v
    return headers


def _list_urls(provider_type: ModelProviderType, base_url: str) -> list[str]:
    base = (base_url or DEFAULT_BASE_URLS.get(provider_type, "")).strip().rstrip("/")
    if not base:
        return []
    if provider_type is ModelProviderType.GEMINI:
        if base.endswith("/models"):
            return [base]
        if base.endswith("/v1beta") or base.endswith("/v1"):
            return [f"{base}/models"]
        return [f"{base}/v1beta/models", f"{base}/v1/models"]
    if base.endswith("/models"):
        return [base]
    if base.endswith("/v1"):
        return [f"{base}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def _ollama_url(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/tags"


def _extract_items(body: Any) -> list[dict]:
    if not isinstance(body, dict):
        return []
    raw = body.get("data") or body.get("models") or []
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            items.append({"id": item})
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
            if not mid:
                continue
            if isinstance(mid, str) and mid.startswith("models/"):
                mid = mid[len("models/"):]
            merged = dict(item)
            merged["id"] = mid
            items.append(merged)
    return items


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]):
    # 只传 url 与 headers：保持与测试里的假 client 签名兼容
    return await client.get(url, headers=headers)


async def discover_models(provider_type: ModelProviderType, base_url: str,
                          api_key: str = "", config: dict | None = None,
                          *, allow_private: bool = True) -> DiscoveryResult:
    """拉取上游模型清单，并附带类型与默认配置。

    失败语义与旧接口一致：返回 `ok=False + error`，不抛 500、不回显上游 body。
    """
    cfg = config or {}
    base = (base_url or DEFAULT_BASE_URLS.get(provider_type, "")).strip()
    if not base:
        return DiscoveryResult(ok=False, error="缺少 Base URL")
    # 不安全地址是调用方错误（400），不是"上游暂时不可用"（200 + error）。
    # 这里直接抛出，让各端点保持既有的 400 契约。
    await validate_outbound_url_async(base, allow_private=allow_private)

    headers = auth_headers(provider_type, api_key,
                           cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None)
    urls = _list_urls(provider_type, base)
    last_error = "无法连接该 base_url"
    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, trust_env=False,
                                     follow_redirects=False) as client:
            for url in urls:
                try:
                    resp = await _get(client, url, headers)
                except httpx.ConnectError:
                    last_error = "无法连接该 base_url"
                    continue
                except httpx.TimeoutException:
                    last_error = "连接上游超时"
                    continue
                if resp.status_code == 401 or resp.status_code == 403:
                    return DiscoveryResult(ok=False,
                                           error="认证失败（401/403）：请检查 API Key")
                if resp.status_code == 404:
                    last_error = "上游没有模型列表接口（404）"
                    continue
                if resp.status_code != 200:
                    last_error = f"上游返回 HTTP {resp.status_code}"
                    continue
                try:
                    body = resp.json()
                except ValueError:
                    last_error = "上游返回的不是 JSON"
                    continue
                items = _extract_items(body)
                if not items and provider_type is ModelProviderType.GEMINI:
                    # Gemini 的 key 放在 header；部分中转站只认 query，但带 key
                    # 的 URL 会进日志，故不降级，直接报无模型。
                    last_error = "上游未返回任何模型"
                    continue
                seen: set[str] = set()
                models: list[dict] = []
                for item in items:
                    mid = str(item["id"])
                    if mid in seen:
                        continue
                    seen.add(mid)
                    models.append(classify_model(mid, item, provider_type))
                if models:
                    return DiscoveryResult(ok=True, models=sorted(
                        models, key=lambda m: (m["model_type"], m["id"])),
                        base_url=base)
                last_error = "上游未返回任何模型"

            # OpenAI 兼容地址可能是 Ollama：/v1/models 不一定实现，
            # 原生 /api/tags 才是事实源。只做一次兜底，不重复发请求。
            if provider_type is ModelProviderType.OPENAI_COMPAT:
                ollama = _ollama_url(base)
                if ollama:
                    try:
                        resp = await _get(client, ollama, {})
                        if resp.status_code == 200:
                            models = [classify_model(str(i["id"]), i, provider_type)
                                      for i in _extract_items(resp.json())]
                            if models:
                                return DiscoveryResult(
                                    ok=True,
                                    models=sorted(models, key=lambda m: (m["model_type"], m["id"])),
                                    base_url=base)
                    except Exception as exc:  # noqa: BLE001 — 兜底失败不改变主结论
                        logger.info("Ollama 兜底 /api/tags 未生效：%s", exc)
    except OutboundURLRejected:
        # 请求阶段被出站闸门拦截（如重定向到云元数据地址）属于调用方错误，
        # 必须冒泡成 400，不能退化成 200 + error 让调用方以为只是上游抽风。
        raise
    except Exception as exc:  # noqa: BLE001 — 发现失败不该变成 500
        return DiscoveryResult(
            ok=False, error=f"{type(exc).__name__}: {str(exc)[:150]}")
    return DiscoveryResult(ok=False, error=last_error)


def provider_default_config(provider_type: ModelProviderType) -> dict:
    """供应商级默认配置（保存时作为初始值，前端可改）。"""
    if provider_type is ModelProviderType.ANTHROPIC:
        return {"auth": "x-api-key", "api_version": "2023-06-01",
                "list_path": "/v1/models", "timeout": 120}
    if provider_type is ModelProviderType.GEMINI:
        return {"auth": "x-goog-api-key", "list_path": "/v1beta/models",
                "timeout": 120}
    return {"auth": "bearer", "list_path": "/v1/models", "timeout": 120}
