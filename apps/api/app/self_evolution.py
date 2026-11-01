"""CEO 分级自我进化：日微调 / 周重构 / 月迭代。

## 一句话

把「CEO 会反思」变成**可审计、可回滚、可被老板叫停的版本化成长**：
每档进化读一段真实运行数据 → 给出确定性诊断 → 生成/升版运行人格
（playbook）→ 落一条记录。没有数据就跳过，绝不为了"看起来在成长"烧模型钱。

## 三档的边界

* **日（daily）**：只增删原则（最多 3 条），不改人格、不动参数。
  一天的样本量不足以换性格，但足够记一条教训。
* **周（weekly）**：结合周考与一周数据，重写原则、更新人格、调整参数
  （质量线/成本守护/并发/补跑预算，全部有硬边界）。
* **月（monthly）**：只产出迭代提案。低风险动作随记录生效；组织/预算这类
  高风险动作进入待拍板，老板批准后才写进「本月重点」。

## 为什么分数是确定性的

诊断分（assessment.score）完全由真实指标算出，模型只负责写叙事与建议。
同一批数据永远得到同一个诊断——老板能自己复算，复盘时不会各说各话。

## 人物化不等于乱改代码

playbook 只影响 CEO 的**提示词与安全边界内的参数**；它不会自动改组织、
不会自动加预算、不会关闭治理。真正不可逆的动作一律走人工拍板。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.logging_config import get_logger
from app.models.ceo import CeoMission
from app.models.evolution import (CeoPlaybook, EvolutionLevel, EvolutionRecord,
                                  EvolutionStatus)
from app.models.task import AgentExecution, Task, TaskStatus
from app.models.token_usage import TokenUsage
from app.models.workflow import WorkflowDefinition, WorkflowFeedback

logger = get_logger(__name__)

LEVELS = (EvolutionLevel.DAILY, EvolutionLevel.WEEKLY,
          EvolutionLevel.MONTHLY)

LEVEL_LABEL = {EvolutionLevel.DAILY: "每日微调",
               EvolutionLevel.WEEKLY: "每周深度复盘",
               EvolutionLevel.MONTHLY: "每月迭代"}

# 参数硬边界：模型可以建议，但只能在这个范围内被采纳。
ADJUSTMENT_BOUNDS = {
    "quality_bar": (60, 95),
    "cost_guard_ratio": (0.6, 1.5),
    "parallel_cap": (2, 6),
    "retry_budget": (0, 2),
}
DEFAULT_ADJUSTMENTS = {"quality_bar": 80, "cost_guard_ratio": 1.0,
                       "parallel_cap": 3, "retry_budget": 1}

PLAYBOOK_SYSTEM = (
    "你是企业 CEO 的成长教练。只输出一个 JSON 对象，不要解释、不要 markdown。"
)

DAILY_PROMPT = """这是 CEO 的「每日微调」输入。请只产出 1-3 条**具体、可执行**的原则
（每条不超过 60 字），不要改人格、不要改参数。

运行数据：{metrics}
确定性诊断：{diagnosis}
现有原则（可保留、可替换其中最弱的一条）：{principles}

输出 JSON：
{{"principles": ["..."], "summary": "一句话说明今天学到了什么"}}"""

WEEKLY_PROMPT = """这是 CEO 的「每周深度复盘」输入。请基于一周真实运行数据与周考结果，
给出一版新的运行人格。原则 6-10 条；参数只能在此基础上小幅调整。

运行数据：{metrics}
确定性诊断：{diagnosis}
周考结果：{exam}
现有原则：{principles}
当前参数：{adjustments}

输出 JSON：
{{"persona": {{"risk_posture": "进取型|稳健型|保守型", "decision_style": "一句话", "traits": ["..."]}},
  "principles": ["..."],
  "adjustments": {{"quality_bar": 80, "cost_guard_ratio": 1.0, "parallel_cap": 3, "retry_budget": 1}},
  "summary": "本周成长总结"}}"""

MONTHLY_PROMPT = """这是 CEO 的「每月迭代」输入。请给出 3-5 项迭代动作。
高风险动作（组织调整、预算调整、放宽治理）必须标 risk=high，只能作为提案等老板拍板；
其余标 risk=low，可随记录生效。

运行数据：{metrics}
确定性诊断：{diagnosis}
周考/月度趋势：{exam}
现有原则：{principles}

输出 JSON：
{{"actions": [{{"type": "org|workflow|quality|cost|training|governance",
                "title": "动作标题",
                "detail": "为什么做、怎么做、预期效果",
                "risk": "low|high"}}],
  "summary": "本月迭代总结"}}"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_month(dt: datetime) -> datetime:
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1, hour=0, minute=0,
                      second=0, microsecond=0)


def period_bounds(level: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """返回 (start, end, key)。日=当天、周=ISO 周一、月=当月 1 号。"""
    now = now or _utcnow()
    if level == EvolutionLevel.DAILY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), start.strftime("%Y-%m-%d")
    if level == EvolutionLevel.WEEKLY:
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        iso = now.isocalendar()
        return start, start + timedelta(days=7), f"{iso.year}-W{iso.week:02d}"
    if level == EvolutionLevel.MONTHLY:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, _next_month(start), start.strftime("%Y-%m")
    raise ValueError(f"未知进化档位：{level}")


# ---------------------------------------------------------------------------
# 指标与诊断（确定性）
# ---------------------------------------------------------------------------

async def _task_status_counts(db: AsyncSession, company_id: str,
                              start: datetime, end: datetime) -> dict:
    rows = (await db.execute(
        select(Task.status, func.count())
        .where(Task.company_id == company_id,
               Task.created_at >= start, Task.created_at < end)
        .group_by(Task.status))).all()
    return {(s.value if hasattr(s, "value") else str(s)): int(c) for s, c in rows}


async def _retried_tasks(db: AsyncSession, company_id: str,
                         start: datetime, end: datetime) -> int:
    """同一任务执行过 >=2 次（说明第一次没成、被重试过）。"""
    inner = (select(Task.id)
             .join(AgentExecution, AgentExecution.task_id == Task.id)
             .where(Task.company_id == company_id,
                    Task.created_at >= start, Task.created_at < end)
             .group_by(Task.id)
             .having(func.count(AgentExecution.id) >= 2)
             .subquery())
    return int((await db.execute(
        select(func.count()).select_from(inner))).scalar() or 0)


async def _workflow_stats(db: AsyncSession, company_id: str,
                          start: datetime, end: datetime) -> dict:
    row = (await db.execute(
        select(func.avg(WorkflowFeedback.score), func.count())
        .select_from(WorkflowFeedback)
        .join(WorkflowDefinition,
              WorkflowDefinition.id == WorkflowFeedback.workflow_id)
        .where(WorkflowDefinition.company_id == company_id,
               WorkflowFeedback.created_at >= start,
               WorkflowFeedback.created_at < end))).first()
    avg_score = float(row[0]) if row and row[0] is not None else None
    runs = int(row[1] or 0) if row else 0
    success = (await db.execute(
        select(func.count())
        .select_from(WorkflowFeedback)
        .join(WorkflowDefinition,
              WorkflowDefinition.id == WorkflowFeedback.workflow_id)
        .where(WorkflowDefinition.company_id == company_id,
               WorkflowFeedback.created_at >= start,
               WorkflowFeedback.created_at < end,
               WorkflowFeedback.success.is_(True)))).scalar() or 0
    return {"avg_score": round(avg_score, 1) if avg_score is not None else None,
            "runs": runs, "success_runs": int(success)}


async def _tokens(db: AsyncSession, company_id: str,
                  start: datetime, end: datetime) -> dict:
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.created_at >= start,
               TokenUsage.created_at < end))).first()
    return {"tokens": int(row[0] or 0), "cost": round(float(row[1] or 0.0), 4)}


async def _lessons(db: AsyncSession, company_id: str,
                   start: datetime, end: datetime, limit: int = 8) -> list[str]:
    rows = (await db.execute(
        select(WorkflowFeedback.lessons)
        .select_from(WorkflowFeedback)
        .join(WorkflowDefinition,
              WorkflowDefinition.id == WorkflowFeedback.workflow_id)
        .where(WorkflowDefinition.company_id == company_id,
               WorkflowFeedback.created_at >= start,
               WorkflowFeedback.created_at < end)
        .order_by(WorkflowFeedback.created_at.desc()).limit(20))).scalars().all()
    out: list[str] = []
    for group in rows:
        for text in (group or []):
            text = str(text).strip()[:200]
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                return out
    return out


async def _latest_exam(db: AsyncSession, company_id: str) -> dict | None:
    """周考在另一个模块里；未部署该模块时安静返回 None，不制造假数据。"""
    try:
        from app.models.exam import CeoExam
    except Exception:  # noqa: BLE001 — 模块尚未落地时进化照常跑
        return None
    row = (await db.execute(
        select(CeoExam).where(CeoExam.company_id == company_id)
        .order_by(CeoExam.created_at.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    return {"id": row.id, "overall": row.overall, "grade": row.grade,
            "personality": row.personality or {},
            "scores": row.scores or {},
            "week_start": row.week_start}


async def _prev_record(db: AsyncSession, company_id: str, level: str) -> dict | None:
    row = (await db.execute(
        select(EvolutionRecord)
        .where(EvolutionRecord.company_id == company_id,
               EvolutionRecord.level == level)
        .order_by(EvolutionRecord.created_at.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    return {"id": row.id, "period_key": row.period_key,
            "assessment": row.assessment or {}, "playbook_version": row.playbook_version}


async def collect_metrics(db: AsyncSession, company_id: str,
                          start: datetime, end: datetime) -> dict:
    missions = (await db.execute(
        select(CeoMission.status, func.count())
        .where(CeoMission.company_id == company_id,
               CeoMission.created_at >= start, CeoMission.created_at < end)
        .group_by(CeoMission.status))).all()
    mission_counts = {s: int(c) for s, c in missions}
    tasks = await _task_status_counts(db, company_id, start, end)
    retried = await _retried_tasks(db, company_id, start, end)
    workflows = await _workflow_stats(db, company_id, start, end)
    tokens = await _tokens(db, company_id, start, end)
    exam = await _latest_exam(db, company_id)
    lessons = await _lessons(db, company_id, start, end)
    return {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "missions": mission_counts,
        "tasks": tasks,
        "retried_tasks": retried,
        "workflows": workflows,
        "tokens": tokens,
        "exam": exam,
        "lessons": lessons,
    }


def has_activity(metrics: dict) -> bool:
    return bool(metrics.get("missions") or metrics.get("tasks")
                or metrics.get("tokens", {}).get("tokens"))


def assess(metrics: dict, level: str) -> dict:
    """确定性诊断：分数只由真实指标算出，模型不参与。"""
    tasks = metrics.get("tasks") or {}
    total = sum(tasks.values())
    completed = int(tasks.get("COMPLETED", 0))
    failed = int(tasks.get("FAILED", 0))
    cancelled = int(tasks.get("CANCELLED", 0))
    active = total - completed - failed - cancelled

    missions = metrics.get("missions") or {}
    m_total = sum(missions.values())
    m_done = int(missions.get("COMPLETED", 0))
    m_failed = int(missions.get("FAILED", 0))
    m_active = m_total - m_done - m_failed

    completion = (completed / total) if total else 1.0
    failure_rate = (failed / total) if total else 0.0
    retry_rate = (metrics.get("retried_tasks", 0) / total) if total else 0.0
    avg_score = (metrics.get("workflows") or {}).get("avg_score")
    exam = metrics.get("exam") or {}
    exam_overall = exam.get("overall")

    score = 100.0
    issues: list[str] = []
    wins: list[str] = []

    if total:
        score -= failure_rate * 40 + retry_rate * 20
        if failure_rate > 0.2:
            issues.append(f"任务失败率 {failure_rate:.0%}（{failed}/{total}）偏高")
        if retry_rate > 0.15:
            issues.append(f"{metrics.get('retried_tasks', 0)} 条任务执行了至少两次，"
                          "说明首次成功率不足")
        if completion >= 0.8 and failed == 0:
            wins.append(f"完成率 {completion:.0%}，本期无失败任务")
    if avg_score is not None:
        if avg_score < 80:
            score -= (80 - avg_score) * 0.5
            issues.append(f"工作流平均分 {avg_score}，低于 80 分升版线")
        elif avg_score >= 90:
            wins.append(f"工作流平均分 {avg_score}，已验证流程稳定")
    if exam_overall is not None:
        if exam_overall < 75:
            score -= (75 - exam_overall) * 0.3
            issues.append(f"周考综合分 {exam_overall}，需要针对性补强")
        elif exam_overall >= 90:
            wins.append(f"周考综合分 {exam_overall}，决策与成本控制稳定")
    if m_failed:
        issues.append(f"{m_failed} 场统筹失败，需回看拆解与验收标准")

    score = round(max(0.0, min(100.0, score)), 1)
    if score >= 90:
        headline = "运行稳健，保持当前节奏"
    elif score >= 75:
        headline = "基本可控，有明确改进点"
    elif score >= 60:
        headline = "存在结构性问题，需要本周纠偏"
    else:
        headline = "风险偏高，建议老板介入"

    return {
        "score": score, "headline": headline,
        "level": level,
        "completion_rate": round(completion, 3),
        "failure_rate": round(failure_rate, 3),
        "retry_rate": round(retry_rate, 3),
        "tasks": {"total": total, "completed": completed, "failed": failed,
                  "cancelled": cancelled, "active": active},
        "missions": {"total": m_total, "completed": m_done,
                     "failed": m_failed, "active": m_active},
        "issues": issues[:6], "wins": wins[:6],
    }


def _diagnosis_text(assessment: dict) -> str:
    lines = [f"【{assessment['score']} 分】{assessment['headline']}",
             f"任务：{assessment['tasks']['completed']} 完成 / "
             f"{assessment['tasks']['failed']} 失败 / "
             f"{assessment['tasks']['total']} 总数；"
             f"统筹：{assessment['missions']['completed']} 完成 / "
             f"{assessment['missions']['failed']} 失败。"]
    if assessment["wins"]:
        lines.append("亮点：" + "；".join(assessment["wins"]))
    if assessment["issues"]:
        lines.append("问题：" + "；".join(assessment["issues"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模型叙事（失败即退到确定性方案，不阻塞进化）
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        if m:
            text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _llm_json(db: AsyncSession, company_id: str, prompt: str,
                    max_tokens: int = 1600) -> tuple[dict | None, dict]:
    """调 CEO 的模型产出一段 JSON；返回 (数据, 本次用量)。"""
    from app.ceo_engine import pick_coordinator
    from app.model_gateway import ModelGateway, usage_context

    try:
        agent = await pick_coordinator(db, company_id)
    except Exception as exc:  # noqa: BLE001 — 没有 CEO 就没有叙事，退确定性方案
        logger.info("进化叙事跳过：company=%s 没有可用 CEO（%s）", company_id, exc)
        return None, {"tokens": 0, "cost": 0.0}
    started = _utcnow()
    try:
        with usage_context(company_id=company_id, agent=str(agent.id),
                           scenario="LEARNING"):
            res = await ModelGateway(db).chat_with_agent_models_by_agent(
                agent,
                [{"role": "system", "content": PLAYBOOK_SYSTEM},
                 {"role": "user", "content": prompt}],
                config={"max_tokens": max_tokens})
    except Exception as exc:  # noqa: BLE001 — 模型不可用不拖垮进化
        logger.warning("进化叙事失败 company=%s: %s: %s",
                       company_id, type(exc).__name__, exc)
        return None, {"tokens": 0, "cost": 0.0}
    usage = res.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.agent_id == str(agent.id),
               TokenUsage.scenario == "LEARNING",
               TokenUsage.created_at >= started))).first()
    cost = float(row[1] or 0.0)
    tokens = max(tokens, int(row[0] or 0))
    return _extract_json(res.get("content") or ""), {
        "tokens": tokens, "cost": round(cost, 4)}


# ---------------------------------------------------------------------------
# playbook
# ---------------------------------------------------------------------------

async def active_playbook(db: AsyncSession, company_id: str) -> CeoPlaybook | None:
    return (await db.execute(
        select(CeoPlaybook)
        .where(CeoPlaybook.company_id == company_id,
               CeoPlaybook.active.is_(True))
        .order_by(CeoPlaybook.version.desc()).limit(1))).scalars().first()


def playbook_block(pb: CeoPlaybook | None) -> str:
    if pb is None:
        return ""
    persona = pb.persona or {}
    lines = [f"CEO 运行人格（第 {pb.version} 版，来源：{pb.source}）："]
    if persona.get("risk_posture"):
        lines.append(f"- 风险姿态：{persona['risk_posture']}")
    if persona.get("decision_style"):
        lines.append(f"- 决策风格：{persona['decision_style']}")
    traits = persona.get("traits") or []
    if traits:
        lines.append("- 行为特征：" + "、".join(str(t) for t in traits[:5]))
    principles = [str(p) for p in (pb.principles or []) if str(p).strip()]
    if principles:
        lines.append("- 运行原则：")
        lines.extend(f"  {i}. {p}" for i, p in enumerate(principles, 1))
    focus = (pb.adjustments or {}).get("monthly_focus") or []
    if focus:
        lines.append("- 本月重点：" + "；".join(str(f) for f in focus[:5]))
    return "\n".join(lines)


async def playbook_text(db: AsyncSession, company_id: str) -> str:
    try:
        return playbook_block(await active_playbook(db, company_id))
    except Exception as exc:  # noqa: BLE001 — 人格注入失败不阻塞主链路
        logger.warning("读取 CEO 运行人格失败 company=%s: %s", company_id, exc)
        return ""


def playbook_brief(pb: CeoPlaybook) -> dict:
    return {
        "id": pb.id, "company_id": pb.company_id, "agent_id": pb.agent_id,
        "version": pb.version, "active": bool(pb.active), "source": pb.source,
        "parent_version": pb.parent_version,
        "persona": pb.persona or {}, "principles": pb.principles or [],
        "adjustments": pb.adjustments or {}, "summary": pb.summary,
        "lessons": pb.lessons or [], "evolution_id": pb.evolution_id,
        "created_at": pb.created_at.isoformat() if pb.created_at else None,
    }


def _clean_principles(items, prev: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for text in list(items or []) + list(prev or []):
        text = str(text).strip()[:120]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _clean_adjustments(raw, prev: dict | None) -> dict:
    base = dict(DEFAULT_ADJUSTMENTS)
    base.update({k: v for k, v in (prev or {}).items() if k in ADJUSTMENT_BOUNDS})
    for key, (lo, hi) in ADJUSTMENT_BOUNDS.items():
        if not isinstance(raw, dict) or key not in raw:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            continue
        value = max(lo, min(hi, value))
        base[key] = int(value) if key in ("parallel_cap", "retry_budget",
                                          "quality_bar") else round(value, 2)
    # 月度重点不允许被模型顺手覆盖
    focus = (prev or {}).get("monthly_focus")
    if focus:
        base["monthly_focus"] = focus
    return base


def _fallback_principles(assessment: dict, lessons: list[str],
                         level: str) -> list[str]:
    """模型不可用时的确定性原则：直接来自诊断与真实教训。"""
    out: list[str] = []
    tasks = assessment["tasks"]
    if tasks["failed"]:
        out.append("每次失败都要留下可检索的根因，并在下一次同类任务前先读一遍。")
    if assessment["retry_rate"] > 0.15:
        out.append("同一条任务连续两次失败时停止硬试，先拆小或换方法再重试。")
    if (assessment["completion_rate"] or 0) < 0.8:
        out.append("拆解时把验收标准写成可判定的句子，避免交付后才发现偏离。")
    if assessment["missions"]["failed"]:
        out.append("统筹失败先回看准绳是否清晰，再决定换人还是补跑。")
    for text in lessons[:3]:
        out.append(f"记住这条教训：{text[:100]}")
    if not out:
        out.append("保持当前节奏，优先复用已验证的工作流而不是重新试错。")
    if level == EvolutionLevel.DAILY:
        out = out[:3]
    return out


def _fallback_monthly_actions(assessment: dict, lessons: list[str]) -> list[dict]:
    actions: list[dict] = []
    if assessment["tasks"]["failed"] or assessment["retry_rate"] > 0.15:
        actions.append({
            "type": "workflow", "title": "为高频失败任务固化成功工作流",
            "detail": ("挑选本期失败/重试最多的任务类型，沉淀成带验收点的"
                       "可复用工作流，下一次直接套用而不是重新试错。"),
            "risk": "low"})
    if (assessment["tasks"]["total"] or 0) and assessment["completion_rate"] < 0.8:
        actions.append({
            "type": "quality", "title": "提高拆解与验收的严格度",
            "detail": "把验收标准改成可判定条目，复盘中逐条核对，降低偏离率。",
            "risk": "low"})
    if lessons:
        actions.append({
            "type": "training", "title": "把本期教训做成定向训练",
            "detail": "用最近的真实失败案例训练相关岗位，避免同类问题复发。",
            "risk": "low"})
    if assessment["tasks"]["failed"]:
        actions.append({
            "type": "org", "title": "按失败归因调整编制与分工",
            "detail": ("如果失败集中在某几个岗位或任务类型，考虑补充专职岗位"
                       "或调整汇报线。涉及组织变更，需老板拍板。"),
            "risk": "high"})
    if not actions:
        actions.append({
            "type": "quality", "title": "扩大已验证工作流的适用范围",
            "detail": "把本期高分工作流复用到相邻任务，继续观察成功率。",
            "risk": "low"})
    return actions[:5]


async def _create_playbook(db: AsyncSession, company_id: str, *,
                           prev: CeoPlaybook | None, source: str,
                           persona: dict, principles: list[str],
                           adjustments: dict, summary: str,
                           lessons: list[str], evolution_id: str,
                           created_by: str | None) -> CeoPlaybook:
    if prev is not None:
        await db.execute(
            update(CeoPlaybook)
            .where(CeoPlaybook.id == prev.id)
            .values(active=False)
            .execution_options(synchronize_session=False))
    pb = CeoPlaybook(
        id=str(uuid.uuid4()), company_id=company_id,
        agent_id=(prev.agent_id if prev is not None else None),
        version=(prev.version + 1 if prev is not None else 1),
        active=True, source=source,
        parent_version=(prev.version if prev is not None else None),
        persona=persona or {}, principles=principles or [],
        adjustments=adjustments or {}, summary=summary or "",
        lessons=(lessons or [])[:10], evolution_id=evolution_id,
        created_by=created_by)
    db.add(pb)
    await db.flush()
    return pb


def _action_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_actions(raw: list | None, fallback: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in (raw or fallback):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:120]
        if not title:
            continue
        risk = "high" if str(item.get("risk") or "").lower() == "high" else "low"
        out.append({
            "id": _action_id(),
            "type": str(item.get("type") or "quality")[:20],
            "title": title,
            "detail": str(item.get("detail") or "").strip()[:1000],
            "risk": risk,
            # 低风险随记录生效；高风险等老板拍板
            "applied": risk == "low",
            "approved": risk == "low",
            "decided_by": None, "decided_at": None, "note": None,
        })
        if len(out) >= 5:
            break
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def _existing(db: AsyncSession, company_id: str, level: str,
                    period_key: str) -> EvolutionRecord | None:
    return (await db.execute(
        select(EvolutionRecord)
        .where(EvolutionRecord.company_id == company_id,
               EvolutionRecord.level == level,
               EvolutionRecord.period_key == period_key)
        .limit(1))).scalars().first()


async def _emergency_stopped(db: AsyncSession, company_id: str) -> bool:
    from app.models.p3_governance import AutonomousSettings
    row = (await db.execute(
        select(AutonomousSettings).where(
            AutonomousSettings.company_id == company_id,
            AutonomousSettings.scope_type == "COMPANY")
        .limit(1))).scalars().first()
    return bool(row and row.emergency_stopped)


def record_brief(rec: EvolutionRecord) -> dict:
    return {
        "id": rec.id, "company_id": rec.company_id, "level": rec.level,
        "period_key": rec.period_key,
        "period_start": rec.period_start.isoformat() if rec.period_start else None,
        "period_end": rec.period_end.isoformat() if rec.period_end else None,
        "status": rec.status, "assessment": rec.assessment or {},
        "diagnosis": rec.diagnosis, "summary": rec.summary,
        "actions": rec.actions or [],
        "metrics": rec.metrics or {},
        "playbook_id": rec.playbook_id, "playbook_version": rec.playbook_version,
        "exam_id": rec.exam_id, "error": rec.error,
        "decided_by": rec.decided_by, "decided_at": rec.decided_at,
        "decision_note": rec.decision_note,
        "tokens_used": rec.tokens_used, "cost": rec.cost,
        "source": rec.source,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


async def run_evolution(db: AsyncSession, company_id: str, level: str,
                        user_id: str | None = None,
                        source: str = "schedule") -> dict:
    """执行一档进化。同周期已存在时直接返回旧记录（幂等）。"""
    if level not in {lv.value for lv in LEVELS}:
        raise ValueError(f"未知进化档位：{level}")
    start, end, key = period_bounds(level)
    existing = await _existing(db, company_id, level, key)
    if existing is not None:
        return {**record_brief(existing), "reused": True}

    rec = EvolutionRecord(
        id=str(uuid.uuid4()), company_id=company_id, level=level,
        period_key=key, period_start=start, period_end=end,
        status=EvolutionStatus.SKIPPED.value, source=source)
    db.add(rec)
    try:
        await db.flush()
    except IntegrityError:
        # 并发触发（调度 + 手动补跑）撞上唯一约束：后到者直接复用先到者的结果，
        # 不能把同一段运行数据进化两遍、多花一次模型钱。
        await db.rollback()
        raced = await _existing(db, company_id, level, key)
        if raced is not None:
            return {**record_brief(raced), "reused": True}
        raise

    async def _skip(reason: str) -> dict:
        rec.status = EvolutionStatus.SKIPPED.value
        rec.error = reason
        rec.diagnosis = reason
        await db.commit()
        await db.refresh(rec)
        return {**record_brief(rec), "reused": False}

    if await _emergency_stopped(db, company_id):
        return await _skip("自治已被老板紧急停止，本轮进化跳过")

    metrics = await collect_metrics(db, company_id, start, end)
    rec.metrics = metrics
    rec.exam_id = (metrics.get("exam") or {}).get("id")
    if not has_activity(metrics):
        return await _skip("本期没有任何运行数据，跳过（不为空地烧模型钱）")

    assessment = assess(metrics, level)
    diagnosis = _diagnosis_text(assessment)
    rec.assessment = assessment
    rec.diagnosis = diagnosis
    lessons = list(metrics.get("lessons") or [])
    prev_pb = await active_playbook(db, company_id)

    if level == EvolutionLevel.MONTHLY:
        prompt = MONTHLY_PROMPT.format(
            metrics=json.dumps(metrics, ensure_ascii=False, default=str)[:6000],
            diagnosis=diagnosis,
            exam=json.dumps(metrics.get("exam") or {}, ensure_ascii=False,
                            default=str)[:1500],
            principles=json.dumps((prev_pb.principles if prev_pb else []),
                                  ensure_ascii=False)[:2000])
        data, usage = await _llm_json(db, company_id, prompt)
        actions = _normalize_actions(
            (data or {}).get("actions") if data else None,
            _fallback_monthly_actions(assessment, lessons))
        rec.actions = actions
        rec.summary = str((data or {}).get("summary") or diagnosis)[:2000]
        rec.tokens_used = usage["tokens"]
        rec.cost = usage["cost"]
        pending = [a for a in actions if not a["applied"]]
        rec.status = (EvolutionStatus.PENDING_APPROVAL.value if pending
                      else EvolutionStatus.APPLIED.value)
        await db.flush()
        await _announce(db, rec, assessment)
        await db.commit()
        await db.refresh(rec)
        return {**record_brief(rec), "reused": False}

    # 日 / 周：产出新一版 playbook
    prev_principles = list(prev_pb.principles or []) if prev_pb else []
    prev_adjust = dict(prev_pb.adjustments or {}) if prev_pb else {}
    prev_persona = dict(prev_pb.persona or {}) if prev_pb else {}
    if level == EvolutionLevel.DAILY:
        prompt = DAILY_PROMPT.format(
            metrics=json.dumps(metrics, ensure_ascii=False, default=str)[:5000],
            diagnosis=diagnosis,
            principles=json.dumps(prev_principles, ensure_ascii=False)[:2000])
    else:
        prompt = WEEKLY_PROMPT.format(
            metrics=json.dumps(metrics, ensure_ascii=False, default=str)[:7000],
            diagnosis=diagnosis,
            exam=json.dumps(metrics.get("exam") or {}, ensure_ascii=False,
                            default=str)[:1500],
            principles=json.dumps(prev_principles, ensure_ascii=False)[:2000],
            adjustments=json.dumps(prev_adjust or DEFAULT_ADJUSTMENTS,
                                   ensure_ascii=False)[:800])
    data, usage = await _llm_json(db, company_id, prompt)
    rec.tokens_used = usage["tokens"]
    rec.cost = usage["cost"]

    if level == EvolutionLevel.DAILY:
        new_principles = _clean_principles(
            (data or {}).get("principles") if data else
            _fallback_principles(assessment, lessons, level),
            prev_principles, limit=10)
        persona = prev_persona
        adjustments = prev_adjust or dict(DEFAULT_ADJUSTMENTS)
        source = "daily"
    else:
        raw_persona = (data or {}).get("persona") if data else None
        persona = {
            "risk_posture": str((raw_persona or {}).get("risk_posture")
                                or prev_persona.get("risk_posture")
                                or "稳健型")[:20],
            "decision_style": str((raw_persona or {}).get("decision_style")
                                  or prev_persona.get("decision_style")
                                  or "先对齐目标，再算成本")[:120],
            "traits": [str(t)[:30] for t in
                       ((raw_persona or {}).get("traits")
                        or prev_persona.get("traits") or [])][:6],
        }
        new_principles = _clean_principles(
            (data or {}).get("principles") if data else
            _fallback_principles(assessment, lessons, level),
            prev_principles, limit=10)
        adjustments = _clean_adjustments(
            (data or {}).get("adjustments") if data else None, prev_adjust)
        source = "weekly"

    summary = str((data or {}).get("summary")
                  or f"{LEVEL_LABEL[level]}：{assessment['headline']}")[:2000]
    pb = await _create_playbook(
        db, company_id, prev=prev_pb, source=source, persona=persona,
        principles=new_principles, adjustments=adjustments, summary=summary,
        lessons=lessons, evolution_id=rec.id, created_by=user_id)
    rec.playbook_id = pb.id
    rec.playbook_version = pb.version
    rec.summary = summary
    rec.status = EvolutionStatus.APPLIED.value
    rec.actions = [{
        "id": _action_id(), "type": "playbook",
        "title": f"运行人格升级到第 {pb.version} 版",
        "detail": summary, "risk": "low", "applied": True, "approved": True,
        "decided_by": user_id, "decided_at": _iso(), "note": None,
    }]
    await _announce(db, rec, assessment)
    await db.commit()
    await db.refresh(rec)
    return {**record_brief(rec), "reused": False}


async def _announce(db: AsyncSession, rec: EvolutionRecord,
                    assessment: dict) -> None:
    """留痕 + 通知。通知失败不影响进化结果。"""
    try:
        from app.services.event_system import log_event
        await log_event(db, "ceo.evolution", "company", rec.company_id,
                        "system", None,
                        {"level": rec.level, "period": rec.period_key,
                         "status": rec.status, "score": assessment.get("score"),
                         "playbook_version": rec.playbook_version},
                        company_id=rec.company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("进化事件留痕失败 record=%s: %s", rec.id, exc)
    if rec.level == EvolutionLevel.DAILY:
        return  # 日报已经够吵，日进化只在记录里可见
    try:
        from app.services.notification_service import notify_company_admins
        label = LEVEL_LABEL[rec.level]
        pending = [a for a in (rec.actions or []) if not a.get("applied")]
        body = (f"{label}完成（{rec.period_key}）："
                f"{assessment.get('score')} 分，{assessment.get('headline')}。"
                + (f" 有 {len(pending)} 项提案待你拍板。" if pending else ""))
        await notify_company_admins(
            db, company_id=rec.company_id, type="ceo.evolution",
            title=f"CEO {label}已生成", body=body[:400], link="/evolution")
    except Exception as exc:  # noqa: BLE001
        logger.warning("进化通知失败 record=%s: %s", rec.id, exc)


async def decide_action(db: AsyncSession, record: EvolutionRecord,
                        action_id: str, *, approve: bool, user_id: str,
                        note: str | None = None) -> dict:
    """老板对月度提案逐项拍板。通过的写进 playbook 的「本月重点」。"""
    actions = [dict(a) for a in (record.actions or [])]
    target = next((a for a in actions if a.get("id") == action_id), None)
    if target is None:
        raise ValueError("动作不存在")
    if target.get("applied"):
        raise ValueError("该动作已生效，无需再审")
    target["approved"] = bool(approve)
    target["applied"] = bool(approve)
    target["decided_by"] = user_id
    target["decided_at"] = _iso()
    target["note"] = (note or "")[:500] or None
    record.actions = actions
    # JSON 列不会追踪原地修改，显式标记才能写回数据库
    flag_modified(record, "actions")
    record.decided_by = user_id
    record.decided_at = _iso()
    record.decision_note = (note or "")[:500] or None

    pending = [a for a in actions if not a.get("applied")]
    if pending:
        record.status = EvolutionStatus.PENDING_APPROVAL.value
    else:
        approved = [a for a in actions if a.get("approved")]
        rejected = [a for a in actions if not a.get("approved")]
        if approved and rejected:
            record.status = EvolutionStatus.PARTIAL.value
        elif approved:
            record.status = EvolutionStatus.APPLIED.value
        else:
            record.status = EvolutionStatus.REJECTED.value

    approved_titles = [a["title"] for a in actions if a.get("approved")]
    if approve and approved_titles:
        prev = await active_playbook(db, record.company_id)
        adjustments = dict((prev.adjustments or {}) if prev is not None
                           else DEFAULT_ADJUSTMENTS)
        adjustments["monthly_focus"] = approved_titles[-5:]
        pb = await _create_playbook(
            db, record.company_id, prev=prev, source="monthly",
            persona=((prev.persona or {}) if prev is not None else {}),
            principles=((prev.principles or []) if prev is not None else []),
            adjustments=adjustments,
            summary=f"老板批准本月迭代：{'；'.join(approved_titles[:3])}",
            lessons=(prev.lessons or [] if prev is not None else []),
            evolution_id=record.id, created_by=user_id)
        record.playbook_id = pb.id
        record.playbook_version = pb.version

    try:
        from app.services.event_system import log_event
        await log_event(db, "ceo.evolution_decided", "company", record.company_id,
                        "user", user_id,
                        {"record_id": record.id, "action_id": action_id,
                         "approve": bool(approve), "status": record.status},
                        company_id=record.company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("进化拍板留痕失败 record=%s: %s", record.id, exc)
    await db.commit()
    await db.refresh(record)
    return record_brief(record)


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------

EVOLUTION_SCHEDULES = (
    ("CEO 每日自我微调", "ceo_evolve_daily", "daily", {"time": "09:10"},
     "CEO 每天从昨天的运行数据里学一点"),
    ("CEO 每周深度复盘", "ceo_evolve_weekly", "weekly",
     {"time": "09:30", "weekday": 0}, "CEO 每周结合周考做结构性复盘"),
    ("CEO 每月迭代", "ceo_evolve_monthly", "monthly",
     {"time": "10:00", "day": 1}, "CEO 每月产出迭代提案，高风险动作等老板拍板"),
)


async def ensure_evolution_schedules(db: AsyncSession, company_id: str,
                                     user_id: str | None) -> dict:
    """幂等建三条进化调度；已存在则原样返回。"""
    from app.models.scheduler import (AutomationSchedule, ScheduleStatus,
                                      compute_next_run)

    out: dict[str, dict] = {}
    now = _utcnow()
    for name, action, trigger, cfg, _desc in EVOLUTION_SCHEDULES:
        existing = (await db.execute(
            select(AutomationSchedule).where(
                AutomationSchedule.company_id == company_id,
                AutomationSchedule.action_type == action,
                AutomationSchedule.status != ScheduleStatus.ARCHIVED)
            .limit(1))).scalars().first()
        if existing is not None:
            out[action] = {"schedule_id": existing.id, "created": False}
            continue
        row = AutomationSchedule(
            id=str(uuid.uuid4()), company_id=company_id, name=name,
            trigger_type=trigger, trigger_config=cfg,
            action_type=action, action_params=cfg,
            status=ScheduleStatus.ACTIVE,
            next_run_at=compute_next_run(trigger, cfg, now),
            created_by=user_id)
        db.add(row)
        out[action] = {"schedule_id": row.id, "created": True}
    await db.commit()
    return out
