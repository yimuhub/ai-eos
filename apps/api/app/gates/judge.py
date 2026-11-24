"""模型评审：按 `acceptance` 给产出打分。

机械断言的局限是"只能判形式"，而"结论是否有依据""是否真的回答了问题"
只能靠评审。这里把评审约束成一个**结构化的判断题**，而不是让它自由发挥：

* 只输出 JSON（score / verdict / must_fix）；
* 只判"标准明确要求但没做到"，不因为"还能写得更好"而 fail；
* 评审不可用（超时、解析失败、无可用模型）时**不伪造通过**——返回 available=False，
  由闸门按 on_fail 策略决定放行、转人工或阻断，并记录降级证据。

评审模型用**该节点执行员工的 primary→fallback 顺序**，不另设全局评审模型：
换模型就换评审口味，这件事必须可解释、可追溯到具体员工配置。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import AgentModelConfig

logger = get_logger(__name__)

_MODEL_ORDER = {"primary": 0, "secondary": 1, "fallback": 2}
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

_SYSTEM_PROMPT = """你是验收评审员。唯一职责：判断产出是否满足给定的验收标准。

严格规则：
1. 只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏。
2. 逐条核对验收标准；标准明确要求而产出没做到的，才算缺口。
3. 不要因为"可以写得更好、更长、更漂亮"而判不通过——那不是验收，是改稿。
4. 无法从产出判断某条是否满足时，把它写进 must_fix，说明"无法验证"。

输出格式（三字段，缺一不可）：
{"score": 0.0 到 1.0 的小数, "verdict": "pass" 或 "fail", "must_fix": ["具体缺口，一句话一条"]}"""

_USER_TEMPLATE = """【验收标准】
{acceptance}

【通过阈值】
{threshold}（score 达到阈值且 must_fix 为空才算 pass）

【产出】
{output}"""


@dataclass
class JudgeResult:
    available: bool = False
    score: float | None = None
    passed: bool = False
    must_fix: list[str] = field(default_factory=list)
    model_id: str | None = None
    tokens: int = 0
    latency_ms: int = 0
    error: str = ""


async def _agent_model_ids(db: AsyncSession, agent) -> list[str]:
    """与 agent_runtime 同一套优先级，保证评审口味和执行一致。"""
    q = await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == agent.id))
    cfgs = sorted(q.scalars().all(), key=lambda c: _MODEL_ORDER.get(c.role, 3))
    return [c.model_id for c in cfgs]


def _parse_verdict(raw: str, threshold: float) -> tuple[float, bool, list[str]]:
    """从模型输出里取 JSON。解析不出来就抛，由调用方判 degraded。"""
    m = _JSON_OBJ_RE.search(raw or "")
    if not m:
        raise ValueError("模型未返回 JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("评审结果不是对象")
    score = float(data.get("score", 0.0))
    score = max(0.0, min(1.0, score))
    must_fix = [str(x)[:300] for x in (data.get("must_fix") or [])][:12]
    verdict = str(data.get("verdict") or "").lower()
    passed = bool(verdict == "pass" and score >= threshold and not must_fix)
    return score, passed, must_fix


async def review(
    db: AsyncSession,
    *,
    agent,
    task_text: str,
    output_text: str,
    acceptance: str,
    threshold: float = 0.6,
    company_id: str | None = None,
    task_id: str | None = None,
    timeout_s: float = 45.0,
    max_output_chars: int = 12000,
) -> JudgeResult:
    """跑一次评审。任何异常都收敛成 `available=False`，绝不向上抛。"""
    if agent is None:
        return JudgeResult(error="该节点没有执行员工，跳过模型评审")
    if not (acceptance or "").strip():
        return JudgeResult(error="未配置验收标准")

    model_ids = await _agent_model_ids(db, agent)
    if not model_ids:
        return JudgeResult(error="执行员工未绑定模型，跳过模型评审")

    out = (output_text or "")[:max_output_chars]
    truncated = len(output_text or "") > max_output_chars
    user = _USER_TEMPLATE.format(
        acceptance=acceptance.strip(), threshold=threshold,
        output=out + ("\n\n[产出已截断，仅评审前 %d 字]" % max_output_chars
                      if truncated else ""))

    from app.model_gateway import ModelGateway, usage_context

    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            gateway = ModelGateway(db)
            with usage_context(agent=agent, scenario="GATE",
                               company_id=company_id, task_id=task_id):
                result = await gateway.chat_with_agent_models(
                    model_ids,
                    [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
                )
    except Exception as e:  # 超时 / 无模型 / 网络 / 预算：一律降级，不判死
        logger.warning("闸门评审不可用 agent=%s: %s", getattr(agent, "id", "?"), e)
        return JudgeResult(error=str(e)[:300],
                           latency_ms=int((time.perf_counter() - t0) * 1000))

    raw = result.get("content") if isinstance(result, dict) else str(result)
    usage = (result.get("usage") or {}) if isinstance(result, dict) else {}
    tokens = int(usage.get("total_tokens")
                 or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                 or 0)
    try:
        score, passed, must_fix = _parse_verdict(raw, threshold)
    except Exception as e:
        logger.warning("闸门评审结果无法解析 agent=%s: %s", getattr(agent, "id", "?"), e)
        return JudgeResult(tokens=tokens, model_id=result.get("used_model_id"),
                           error=f"评审结果无法解析：{e}",
                           latency_ms=int((time.perf_counter() - t0) * 1000))

    return JudgeResult(
        available=True, score=score, passed=passed, must_fix=must_fix,
        model_id=result.get("used_model_id"), tokens=tokens,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )
