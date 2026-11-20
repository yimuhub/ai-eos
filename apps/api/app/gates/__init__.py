"""步骤级验收闸门（M1）。

对外只暴露 `enforce`、`GateBlocked` 与 `resolve_gate`：

    outcome = await enforce(db, execution_id=..., node=node, output=out,
                            agent=agent, rework=my_rework_fn)

`enforce` 内部按 机械断言 → 模型评审 → 定向返工 的顺序执行；
不通过且不允许返工时抛 `GateBlocked`，沿用执行器既有的失败路径。
"""
from .judge import JudgeResult, review
from .rules import (Check, GateSpec, KNOWN_RULES, blocking_failures,
                    effective_chars, evaluate, extract_file_blocks, summarize)
from .step_gate import (GateBlocked, GateOutcome, enforce, resolve_gate)

__all__ = [
    "Check", "GateSpec", "GateBlocked", "GateOutcome", "JudgeResult",
    "KNOWN_RULES", "blocking_failures", "effective_chars", "enforce",
    "evaluate", "extract_file_blocks", "resolve_gate", "review", "summarize",
]
