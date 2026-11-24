"""AI 自主率：口径按**次数**，不按工作量。

    autonomy = AI 完成步骤 ÷ (AI 完成步骤 + 人工节点 + 手记介入)

- AI 完成步骤：COMPLETED 且未被返工作废的 AGENT 步骤（模型驱动的工作）；
  HTTP/TOOL/TRANSFORM/SUB_WORKFLOW 是编排或确定性执行，不算 AI 完成的活；
  条件分支、等待、并行等编排节点同样不计；
- 人工节点：APPROVAL / HUMAN_INPUT 的完成步骤；
- 手记介入：`workflow_manual_log` 的条数。

没有样本时返回 None 而不是 0，避免"没数据"被读成"全靠人工"。
"""
from app.models.workflow import NodeType, RunStatus

AI_TYPES = {NodeType.AGENT.value}
HUMAN_TYPES = {NodeType.APPROVAL.value, NodeType.HUMAN_INPUT.value}


def _step_identity(step: dict) -> tuple | None:
    """识别父子流程聚合时重复出现的同一条工作。

    运行时的 AGENT 产出带 task_id；没有该字段的旧数据退回执行 + 节点 +
    attempt。没有身份字段的步骤不能按 node_key 硬去重，否则不同工作流里
    恰好同名的节点会被误合并。
    """
    output = step.get("output_data")
    task_id = step.get("task_id")
    if not task_id and isinstance(output, dict):
        task_id = output.get("task_id")
    if task_id:
        return ("task", str(task_id))
    execution_id = step.get("execution_id")
    if execution_id:
        return ("step", str(execution_id), step.get("node_key"),
                step.get("attempt") or 1)
    return None


def compute_autonomy(steps: list[dict], manual_logs: list[dict]) -> dict:
    ai_steps = human_steps = 0
    seen: set[tuple] = set()
    for s in steps:
        if s.get("status") not in (RunStatus.COMPLETED.value, "COMPLETED"):
            continue
        if s.get("stale_at"):
            continue        # 返工作废的版本不算"AI 完成的活"
        ntype = s.get("node_type")
        if ntype not in AI_TYPES and ntype not in HUMAN_TYPES:
            continue
        identity = _step_identity(s)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        if ntype in AI_TYPES:
            ai_steps += 1
        else:
            human_steps += 1

    logs = len(manual_logs or [])
    total = ai_steps + human_steps + logs
    reasons: dict[str, int] = {}
    for row in manual_logs or []:
        r = str(row.get("reason") or "judgment")
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "ai_steps": ai_steps,
        "human_steps": human_steps,
        "manual_logs": logs,
        "total": total,
        "autonomy_rate": (ai_steps / total) if total else None,
        "manual_reasons": reasons,
        "formula": "ai_steps / (ai_steps + human_steps + manual_logs)",
    }


def merge_autonomy(parts: list[dict], logs: list[dict]) -> dict:
    """把多个执行的账本合成公司口径（分母相加，不平均率）。"""
    ai = sum(p["ai_steps"] for p in parts)
    human = sum(p["human_steps"] for p in parts)
    manual = len(logs or [])
    total = ai + human + manual
    reasons: dict[str, int] = {}
    for row in logs or []:
        r = str(row.get("reason") or "judgment")
        reasons[r] = reasons.get(r, 0) + 1
    return {"executions": len(parts), "ai_steps": ai, "human_steps": human,
            "manual_logs": manual, "total": total,
            "autonomy_rate": (ai / total) if total else None,
            "manual_reasons": reasons,
            "formula": "ai_steps / (ai_steps + human_steps + manual_logs)"}
