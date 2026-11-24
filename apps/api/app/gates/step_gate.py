"""验收闸门编排：机械断言 → 模型评审 → 定向返工 → 证据落库。

一组明确的取舍（都写在代码里，不靠文档口头约定）：

1. **默认不返工**。`max_rework` 缺省 0（只检查、只留痕）。上线初期先观察
   "本该过却被拦"的样本，确认误杀率可接受后再逐个工作流开返工。
2. **返工上限硬编码 1 轮**（配置可写 0，超过 1 一律按 1 处理）。模型有时越改
   越差，多轮返工是烧钱换噪声。
3. **评审不可用按策略处理**。observe 放行 + 留痕；manual/fail 记未通过并阻断，
   报告里标"降级"。不能把"没评上"伪装成"评过了"。
4. **阻断才失败**。只有 `blocking=True` 的断言失败才让节点失败；
   非阻断项只记警告（为后续"软性质量分"留位）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.workflow_gate import WorkflowStepGate

from . import judge as judge_mod
from .rules import (Check, GateSpec, blocking_failures, evaluate,
                    extract_file_blocks, summarize)

logger = get_logger(__name__)

GATE_KEY = "gate"
MAX_REWORK_HARD_CAP = 1
# 单个执行实例的评审 token 上限（机械断言免费，不在此列）。
# 超过后只跑机械断言并标 degraded，防止一个工作流把预算烧在"评审"上。
GATE_TOKEN_CAP = int(os.environ.get("AIEOS_GATE_TOKEN_CAP", "80000"))

_ALLOWED_KEYS = {
    "acceptance", "assert", "asserts", "max_rework", "on_fail",
    "judge_threshold", "allow_cliche", "judge",
}
_ALLOWED_ON_FAIL = {"fail", "manual", "observe"}


class GateBlocked(RuntimeError):
    """节点产出未过闸门。继承 RuntimeError，沿用执行器的失败路径。"""

    def __init__(self, node_key: str, checks: list[Check], reason: str = ""):
        fails = blocking_failures(checks)
        detail = "；".join(f"{c.rule}: {c.actual} / 要求 {c.expected}"
                         for c in fails) or reason or "未通过"
        super().__init__(f"验收未通过（节点 {node_key}）：{detail}")
        self.node_key = node_key
        self.checks = [c.to_dict() for c in checks]


@dataclass
class GateOutcome:
    enabled: bool
    passed: bool
    checks: list[Check]
    degraded: bool = False
    round: int = 0
    # 闸门可能已经用返工产出替换了输入；调用方必须持久化/下发这份有效产出。
    output: dict | None = None


def resolve_gate(node) -> GateSpec | None:
    """读 `node.config["gate"]`。没配就返回 None（等于闸门关闭）。

    配置写错（未知关键字 / on_fail 非法）直接抛，让节点带着明确原因失败——
    静默忽略等于闸门形同虚设，那比报错更危险。
    """
    cfg = (node.config or {}).get(GATE_KEY)
    if not cfg:
        return None
    if not isinstance(cfg, dict):
        raise ValueError(f"节点 {node.node_key} 的 gate 配置必须是对象")

    unknown = set(cfg) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"节点 {node.node_key} 的 gate 含未知字段 {sorted(unknown)}；"
            f"可用：{sorted(_ALLOWED_KEYS)}")

    on_fail = str(cfg.get("on_fail") or "fail")
    if on_fail not in _ALLOWED_ON_FAIL:
        raise ValueError(f"节点 {node.node_key} 的 gate.on_fail 只能是 {sorted(_ALLOWED_ON_FAIL)}")

    try:
        max_rework = int(cfg.get("max_rework") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"节点 {node.node_key} 的 gate.max_rework 必须是整数")
    max_rework = max(0, min(max_rework, MAX_REWORK_HARD_CAP))

    asserts = cfg.get("assert") if "assert" in cfg else cfg.get("asserts")
    if asserts is None:
        asserts = {}
    if not isinstance(asserts, dict):
        raise ValueError(f"节点 {node.node_key} 的 gate.assert 必须是对象")

    # 规则名在配置解析阶段就校验（而不是跑完节点才炸）：
    # 拼错一个字段名应当立刻报错，不能让员工白跑一次再失败。
    from .rules import KNOWN_RULES
    unknown_rules = set(asserts) - KNOWN_RULES
    if unknown_rules:
        raise ValueError(
            f"节点 {node.node_key} 的 gate.assert 含未知规则 {sorted(unknown_rules)}；"
            f"可用：{sorted(KNOWN_RULES)}")

    return GateSpec(
        acceptance=str(cfg.get("acceptance") or "").strip(),
        asserts=dict(asserts),
        max_rework=max_rework,
        on_fail=on_fail,
        judge_threshold=float(cfg.get("judge_threshold") or 0.6),
        allow_cliche=bool(cfg.get("allow_cliche") or False),
    )


def as_text(output: Any) -> str:
    """把节点输出压成"给人看/给规则量"的正文。

    优先取语义字段，避免把 task_id 之类的元数据当正文去字数。
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("output", "content", "result", "text", "markdown"):
            v = output.get(key)
            if isinstance(v, str) and v.strip():
                return v
        # 没有语义字段时退化为整体 JSON（并保证规则度量不会因为字段名不同而失效）
        return json.dumps(output, ensure_ascii=False)
    return str(output)


def as_files(output: Any) -> list[dict]:
    """文件块：优先结构化字段，其次文本标记。统一成 [{name, content}]。"""
    if isinstance(output, dict):
        raw = output.get("files")
        if isinstance(raw, list) and raw:
            norm = []
            for f in raw:
                if isinstance(f, dict):
                    norm.append({"name": str(f.get("name") or f.get("path") or "")[:300],
                                 "content": str(f.get("content") or "")})
                else:
                    norm.append({"name": str(f)[:300], "content": ""})
            return norm
    text = as_text(output)
    return [{"name": n, "content": ""} for n in extract_file_blocks(text)]


async def _gate_token_used(db: AsyncSession, execution_id: str) -> int:
    q = await db.execute(
        select(func.coalesce(func.sum(WorkflowStepGate.tokens), 0))
        .where(WorkflowStepGate.execution_id == execution_id))
    return int(q.scalar() or 0)


def _rework_prompt(node_name: str, previous: str, fails_text: str,
                   user_note: str | None = None) -> str:
    """返工指令：三段固定结构，禁止模型自由发挥。"""
    note = f"\n【额外要求】\n{user_note}\n" if user_note else ""
    return (
        "你在上一轮产出的基础上修改，不要重写。\n\n"
        "【上一版产出】\n"
        f"{previous}\n\n"
        "【未通过的检查项】\n"
        f"{fails_text}\n"
        f"{note}\n"
        "只修这些。其余内容、结构、结论保持原样。"
    )


async def _persist(db: AsyncSession, *, execution_id: str, node_key: str,
                   attempt: int, gate_round: int, checks: list[Check],
                   passed: bool, degraded: bool, score: float | None,
                   judge_model: str | None, tokens: int, latency_ms: int,
                   output_before: str | None, detail: str | None) -> None:
    blocking = bool(blocking_failures(checks))
    db.add(WorkflowStepGate(
        execution_id=execution_id, node_key=node_key, attempt=attempt,
        gate_round=gate_round,
        stage="judge" if judge_model or score is not None else "mechanical",
        checks=[c.to_dict() for c in checks],
        passed=passed, blocking=blocking, degraded=degraded,
        score=score, judge_model=judge_model, tokens=tokens,
        latency_ms=latency_ms, output_before=(output_before or "")[:4000] or None,
        detail=(detail or "")[:2000] or None,
    ))
    await db.flush()


async def enforce(
    db: AsyncSession,
    *,
    execution_id: str,
    node,
    output: dict,
    agent=None,
    attempt: int = 1,
    task_id: str | None = None,
    company_id: str | None = None,
    rework: Callable[[str], Awaitable[dict]] | None = None,
) -> GateOutcome:
    """跑闸门。不通过且允许返工则调用 rework 回调，最多 1 轮。

    返回 GateOutcome；未启用闸门时 `enabled=False`，调用方原样放行。
    最终仍不通过 → 抛 GateBlocked（调用方不需要自己判断）。
    """
    spec = resolve_gate(node)
    if spec is None:
        return GateOutcome(enabled=False, passed=True, checks=[], output=output)

    before_text: str | None = None
    token_cap_hit = False
    judge_skipped = False

    for gate_round in range(0, spec.max_rework + 1):
        t0 = time.perf_counter()
        text = as_text(output)
        files = as_files(output)

        # 1) 机械断言（零成本，先跑）
        checks = evaluate(text, files, spec)

        # 2) 评审（只在机械全过、且配了 acceptance 时跑，省钱）
        degraded = False
        score: float | None = None
        judge_model: str | None = None
        tokens = 0
        if checks_ok(checks) and spec.acceptance:
            used = await _gate_token_used(db, execution_id)
            if used >= GATE_TOKEN_CAP:
                degraded = True
                token_cap_hit = True
                judge_skipped = True
                checks.append(Check(
                    "acceptance", False, spec.judge_threshold,
                    "skipped", blocking=spec.on_fail != "observe",
                    detail=f"评审 token 已达上限 {GATE_TOKEN_CAP}，本步只跑机械断言"))
            else:
                jr = await judge_mod.review(
                    db, agent=agent, task_text=(node.name or node.node_key),
                    output_text=text, acceptance=spec.acceptance,
                    threshold=spec.judge_threshold,
                    company_id=company_id, task_id=task_id)
                score, judge_model, tokens = jr.score, jr.model_id, jr.tokens
                if jr.available:
                    checks.append(Check(
                        "acceptance", jr.passed, spec.judge_threshold, jr.score,
                        detail="；".join(jr.must_fix)[:1000] or "符合验收标准"))
                else:
                    degraded = True
                    judge_skipped = True
                    checks.append(Check(
                        "acceptance", False, spec.judge_threshold,
                        "unavailable", blocking=spec.on_fail != "observe",
                        detail=f"评审降级：{jr.error[:300]}"))

        passed = not blocking_failures(checks) and not judge_skipped
        latency = int((time.perf_counter() - t0) * 1000)

        await _persist(
            db, execution_id=execution_id, node_key=node.node_key, attempt=attempt,
            gate_round=gate_round, checks=checks, passed=passed, degraded=degraded,
            score=score, judge_model=judge_model, tokens=tokens,
            latency_ms=latency, output_before=before_text,
            detail=("评审降级（token 上限）" if token_cap_hit else None))

        if passed:
            return GateOutcome(enabled=True, passed=True, checks=checks,
                               degraded=degraded, round=gate_round, output=output)

        # 3) 不通过：observe 模式只留证、不阻断（上线观察期用）
        if spec.on_fail == "observe":
            return GateOutcome(enabled=True, passed=False, checks=checks,
                               degraded=degraded, round=gate_round, output=output)

        # 还能返工就返工，否则按 on_fail 处理
        can_rework = (gate_round < spec.max_rework and rework is not None
                      and not token_cap_hit)
        if not can_rework:
            if spec.on_fail == "manual":
                raise GateBlocked(
                    node.node_key, checks,
                    reason="需要人工验收：请在任务详情里确认产出后再放行")
            raise GateBlocked(node.node_key, checks)

        before_text = text
        fails_text = summarize(checks)
        logger.info("闸门返工 node=%s round=%d reasons=%s",
                    node.node_key, gate_round, fails_text[:300])
        output = await rework(_rework_prompt(node.name or node.node_key,
                                             text, fails_text))

    # 理论不可达：循环内要么通过要么抛
    raise GateBlocked(node.node_key, checks)


def checks_ok(checks: list[Check]) -> bool:
    return not blocking_failures(checks)

