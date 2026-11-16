"""知识库 embedding 侧：模型配置解析 + OpenAI 兼容 /embeddings 调用（V6 M1-f）。

设计边界（与 knowledge_engine 的「无配置时行为完全不变」承诺配套）：

* **尽力而为，绝不抛异常**：摄入与检索的主链路是关键词检索，向量是增强
  路径。没有配置 embedding 模型、上游不可用、响应格式异常、维度不一致……
  一律返回 ``None``，由调用方退回关键词分数。任何一次失败都不允许拖垮
  摄入或检索。
* **配置来源与模型网关同口径**：只认 ``models`` 表里 ``model_type=
  'embedding'`` 且 ``provider_type='openai_compat'`` 的行（anthropic 没有
  embedding 接口、gemini 请求形状不同，v1 不支持，视为「无可用配置」）；
  base_url / api_key 字段沿用 models 表既有列，**不发明新存储**。密钥解析
  走 ``api_key_resolver.resolve``（公司密钥库 BYOK → 环境变量/.env），与
  ``ModelGateway._provider_for`` 的顺序一致。公司自有行优先，其次平台共享
  行（company_id IS NULL）——与共享聊天模型「租户 BYOK 密钥优先」的语义
  保持一致。
* **HTTP 口径与网关一致**：复用按事件循环缓存的共享 ``AsyncClient``
  （``model_gateway.get_http_client``，trust_env=False、只认
  AIEOS_HTTP_PROXY）；URL 拼法与 OpenAICompatProvider 相同
  （base_url 原样 + ``/embeddings``，/v1 由配置自带）；SSRF 闸门走
  ``net_guard.validate_outbound_url_async(allow_private=True)``——模型服务
  是管理员自己配置的（本地 llama.cpp / Ollama 常在私网），与
  model_providers_api / model_discovery 的口径相同。
* **维度自适应**：不落库维度，以每次调用实际返回为准。同一批次内维度
  必须一致；检索时查询向量与 chunk 向量维度不等（embedding 模型被换过）
  即判该向量不可用，整体退回关键词——旧向量不迁移、不报错，直接失效。
* **刻意不引 sqlite-vec / pgvector**：向量以 JSON 浮点数组存在
  ``kb_chunks.embedding``，候选集已在 SQL 层限流，Python 逐块余弦的量级
  对 v1 完全够用；零新依赖、SQLite/PG 通吃。
* **不重试**：chat 走 ``_post_with_retry`` 是因为用户在等一句回复，多等
  几秒值得；embedding 是锦上添花，失败就该尽快退回关键词路径，退避重试
  只会把摄入/检索挂慢。一次干净的尝试，失败即降级。
"""
from __future__ import annotations

import math

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.model_gateway import get_http_client
from app.models.agent import Model, ModelProviderType
from app.services import api_key_resolver

logger = get_logger(__name__)

# embedding 调用的专用分阶段超时：比 chat（read=120s）紧得多——
# embedding 是毫秒级任务，60s 读超时已经足够宽容，再长只会在
# 上游故障时把摄入/检索拖住。
EMBED_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=15.0, pool=5.0)


async def _resolve_config(db: AsyncSession,
                          company_id: str | None) -> tuple[str, str, str] | None:
    """解析该公司的 embedding 模型配置，返回 (base_url, model_id, api_key)。

    无可用配置返回 None（这是「无配置时行为不变」的第一道闸）。密钥允许
    为空串：本地 OpenAI 兼容网关（Ollama / llama.cpp）常不需要鉴权，与
    OpenAICompatProvider「无 Key 就不带 Authorization 头」的行为一致。
    """
    # 公司自有优先于平台共享；NULL 公司 = 平台级共享模型
    company_scope = (or_(Model.company_id == company_id,
                         Model.company_id.is_(None))
                     if company_id else Model.company_id.is_(None))
    rows = (await db.execute(
        select(Model).where(
            Model.model_type == "embedding",
            Model.provider_type == ModelProviderType.OPENAI_COMPAT,
            company_scope,
            # enabled 可空，NULL 等价 True（与 models 表历史口径一致）
            or_(Model.enabled.is_(None), Model.enabled == True),  # noqa: E712
        ))).scalars().all()
    if not rows:
        return None
    owned = [r for r in rows if r.company_id and r.company_id == company_id]
    row = owned[0] if owned else rows[0]

    base_url = (row.base_url or "").strip()
    model_id = (row.model_id or "").strip()
    if not base_url or not model_id:
        return None

    key = await api_key_resolver.resolve(
        db, company_id=company_id,
        api_key_env=getattr(row, "api_key_env", None),
        provider_type=row.provider_type)
    return base_url, model_id, key.value


async def embed_texts(db: AsyncSession, company_id: str | None,
                      texts: list[str]) -> list[list[float]] | None:
    """批量取文本向量；不可用（无配置/调用失败/响应异常）返回 None。

    一次调用带全部输入（摄入时即整篇文档的 chunk 批）；调用方把失败当
    「这一批没有向量」处理，照常入库/检索。
    """
    try:
        if not texts:
            return []
        cfg = await _resolve_config(db, company_id)
        if cfg is None:
            return None
        base_url, model_id, api_key = cfg

        url = f"{base_url.rstrip('/')}/embeddings"
        try:
            await validate_outbound_url_async(url, allow_private=True)
        except OutboundURLRejected as exc:
            logger.warning("知识库 embedding 跳过（地址未过安全闸门）：%s", exc)
            return None

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = await get_http_client().post(
            url, json={"model": model_id, "input": texts},
            headers=headers, timeout=EMBED_TIMEOUT)
        resp.raise_for_status()
        return _parse_response(resp.json(), len(texts))
    except Exception as exc:  # noqa: BLE001 — 任何失败都降级，绝不拖垮主链路
        logger.warning("知识库 embedding 不可用，降级关键词检索：%s: %s",
                       type(exc).__name__, str(exc)[:200])
        return None


def _parse_response(data: object, expected: int) -> list[list[float]] | None:
    """解析 /embeddings 响应；任何形状异常都返回 None（不抛）。"""
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or len(items) != expected:
        return None
    vectors: list[list[float] | None] = [None] * expected
    for item in items:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        embedding = item.get("embedding")
        if not isinstance(index, int) or not 0 <= index < expected:
            return None
        if not isinstance(embedding, list) or not embedding:
            return None
        if not isinstance(embedding[0], (int, float)):
            return None
        vectors[index] = embedding
    if any(v is None for v in vectors):
        return None
    # 维度自适应的锚点：同批次维度必须一致（模型中途被换会有脏数据，判死）
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        return None
    return vectors  # type: ignore[return-value]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度，归一到 0~1：原始 cos ∈ [-1,1] → (cos+1)/2。

    单调变换不改相对次序，只把分数量纲对齐到关键词分（0~1），线性混合
    时两边权重才有共同尺度。零向量返回 0。
    """
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return (dot / math.sqrt(na * nb) + 1.0) / 2.0


def usable_vector(embedding: object,
                  query_vec: list[float] | None) -> list[float] | None:
    """chunk 的向量「可用于与本次查询算余弦」时返回它，否则 None。

    维度必须与查询向量一致（模型被换过 → 旧向量整体判死），且元素必须是
    数值——向量虽只由本模块写入，但手工改库/外部导入可能塞进脏 JSON；
    检索打分循环没有兜底 except，脏元素会让 cosine 抛 TypeError 直接 500。
    逐元素检查用 type() 判断（比 isinstance 便宜），最坏 500 候选 × 1536 维
    约几十毫秒，远低于一次上游 RPC，换来「脏数据退回关键词分」的确定性。
    """
    if query_vec is None or not isinstance(embedding, list):
        return None
    if len(embedding) != len(query_vec) or not embedding:
        return None
    for x in embedding:
        t = type(x)
        if t is not float and t is not int:
            return None
    return embedding
