"""Performance engine (P2-11): aggregate real metrics from DB."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.collaboration import AgentMessage
from app.models.meeting import MeetingParticipant
from app.models.performance import PerformanceSnapshot
from app.models.task import AgentExecution
from app.models.token_usage import TokenUsage

logger = get_logger(__name__)

_DONE = ("COMPLETED", "SUCCESS", "DONE")
_FAILED = ("FAILED", "ERROR")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _metrics_from(agent_id: str, execs: list, spoke: int, msgs_sent: int,
                  cost_sum: float) -> dict:
    """纯计算：把已取到的执行/会议/消息/成本数据折算成指标。"""
    total = len(execs)
    completed = sum(1 for e in execs if (e.status or "").upper() in _DONE)
    failed = sum(1 for e in execs if (e.status or "").upper() in _FAILED)
    durations = []
    for e in execs:
        if e.created_at and e.updated_at and \
                (e.status or "").upper() in _DONE:
            delta = (e.updated_at - e.created_at).total_seconds()
            if 0 <= delta < 3600:  # 排除异常值
                durations.append(delta)
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    completion_rate = round(completed / total, 3) if total else None
    fail_rate = round(failed / total, 3) if total else None

    # 综合分：完成率 60% + 稳定性(1-fail_rate) 25% + 活跃度 15%
    score = 0.0
    if total:
        cr = completion_rate or 0
        stability = 1 - (fail_rate or 0)
        activity = min(1.0, (total + spoke * 2 + msgs_sent) / 50)
        score = round(cr * 60 + stability * 25 + activity * 15, 1)

    # V5.6.1 考核量化（学 TheAgentCompany）：成本维度 + ROI
    # cost_per_task + roi_30d：完成产出价值 ≈ 完成数 × 基准人工小时(0.5h) × 时薪(100¥/h)，
    # 减去该 Agent 实际 LLM 成本
    cost_per_task = round(cost_sum / completed, 4) if completed else (
        round(cost_sum / total, 4) if total else 0.0)
    output_value = completed * 50.0
    roi = round((output_value - cost_sum) / cost_sum, 3) if cost_sum > 0 else None

    return {
        "agent_id": agent_id,
        "tasks_total": total,
        "tasks_completed": completed,
        "tasks_failed": failed,
        "completion_rate": completion_rate,
        "avg_duration_s": avg_duration,
        "meeting_spoke_count": spoke,
        "messages_sent": msgs_sent,
        "score": score,
        # V5.6.1 ROI 维度
        "cost_total": round(cost_sum, 4),
        "cost_per_task": cost_per_task,
        "output_value_est": round(output_value, 2),
        "roi": roi,
    }


async def _load_metrics(db: AsyncSession, agent_ids: list[str],
                        since: datetime | None = None) -> dict[str, dict]:
    """批量取数：固定 4 条聚合查询，与 Agent 数量无关（消除 N+1）。"""
    if not agent_ids:
        return {}

    exec_q = select(AgentExecution).where(
        AgentExecution.agent_id.in_(agent_ids))
    if since:
        exec_q = exec_q.where(AgentExecution.created_at >= since)
    execs_by: dict[str, list] = {aid: [] for aid in agent_ids}
    for e in (await db.execute(exec_q)).scalars().all():
        execs_by.setdefault(e.agent_id, []).append(e)

    spoke_q = (select(MeetingParticipant.agent_id,
                      func.coalesce(func.sum(MeetingParticipant.spoke_count), 0))
               .where(MeetingParticipant.agent_id.in_(agent_ids))
               .group_by(MeetingParticipant.agent_id))
    spoke_by = {r[0]: int(r[1] or 0)
                for r in (await db.execute(spoke_q)).all()}

    msg_q = (select(AgentMessage.sender_agent_id, func.count())
             .where(AgentMessage.sender_agent_id.in_(agent_ids))
             .group_by(AgentMessage.sender_agent_id))
    msgs_by = {r[0]: int(r[1] or 0)
               for r in (await db.execute(msg_q)).all()}

    cost_by: dict[str, float] = {}
    try:
        c_q = select(TokenUsage.agent_id,
                     func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        c_q = c_q.where(TokenUsage.agent_id.in_(agent_ids))
        if since:
            c_q = c_q.where(TokenUsage.created_at >= since)
        c_q = c_q.group_by(TokenUsage.agent_id)
        cost_by = {r[0]: float(r[1] or 0.0)
                   for r in (await db.execute(c_q)).all()}
    except Exception as e:
        # 成本聚合失败不阻断绩效计算，但 cost/ROI 会是 0，必须留痕
        logger.warning("成本聚合失败（cost/ROI 按 0 计）：%s: %s",
                       type(e).__name__, e, exc_info=True)

    return {
        aid: _metrics_from(aid, execs_by.get(aid, []), spoke_by.get(aid, 0),
                           msgs_by.get(aid, 0), cost_by.get(aid, 0.0))
        for aid in agent_ids
    }


async def compute_agent_metrics(db: AsyncSession, agent_id: str,
                                since: datetime | None = None) -> dict:
    """从真实执行数据计算单个 Agent 的绩效指标。"""
    return (await _load_metrics(db, [agent_id], since))[agent_id]


async def snapshot_company(db: AsyncSession, company_id: str,
                           since: datetime | None = None) -> list[dict]:
    """对公司所有 Agent 计算并保存绩效快照。"""
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    metrics = await _load_metrics(db, [a.id for a in agents], since)
    results = []
    now = _now()
    for a in agents:
        m = metrics[a.id]
        snap = PerformanceSnapshot(id=str(uuid.uuid4()), agent_id=a.id,
                                   company_id=company_id,
                                   period_start=since or datetime(2000, 1, 1),
                                   period_end=now, metrics=m, score=m["score"])
        db.add(snap)
        results.append({"agent_name": a.name, **m})
    await db.commit()
    return sorted(results, key=lambda r: -r["score"])
