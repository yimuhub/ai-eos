"""Performance engine (P2-11): aggregate real metrics from DB."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.collaboration import AgentMessage
from app.models.meeting import MeetingParticipant
from app.models.performance import PerformanceSnapshot
from app.models.task import AgentExecution


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def compute_agent_metrics(db: AsyncSession, agent_id: str,
                                since: datetime | None = None) -> dict:
    """从真实执行数据计算单个 Agent 的绩效指标。"""
    q = select(AgentExecution).where(AgentExecution.agent_id == agent_id)
    if since:
        q = q.where(AgentExecution.created_at >= since)
    execs = (await db.execute(q)).scalars().all()

    total = len(execs)
    completed = sum(1 for e in execs if (e.status or "").upper() in
                    ("COMPLETED", "SUCCESS", "DONE"))
    failed = sum(1 for e in execs if (e.status or "").upper() in
                 ("FAILED", "ERROR"))
    durations = []
    for e in execs:
        if e.created_at and e.updated_at and \
                (e.status or "").upper() in ("COMPLETED", "SUCCESS", "DONE"):
            delta = (e.updated_at - e.created_at).total_seconds()
            if 0 <= delta < 3600:  # 排除异常值
                durations.append(delta)
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    # 会议发言
    part_q = await db.execute(select(MeetingParticipant).where(
        MeetingParticipant.agent_id == agent_id))
    spoke = sum(p.spoke_count or 0 for p in part_q.scalars().all())

    # 协作消息发送量
    msg_q = await db.execute(select(func.count()).select_from(AgentMessage)
                             .where(AgentMessage.sender_agent_id == agent_id))
    msgs_sent = msg_q.scalar() or 0

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
    cost_sum = 0.0
    try:
        from app.models.token_usage import TokenUsage
        c_q = select(func.coalesce(func.sum(TokenUsage.total_cost), 0.0)).where(
            TokenUsage.agent_id == agent_id)
        if since:
            c_q = c_q.where(TokenUsage.created_at >= since)
        cost_sum = float((await db.execute(c_q)).scalar() or 0.0)
    except Exception:
        pass
    cost_per_task = round(cost_sum / completed, 4) if completed else (
        round(cost_sum / total, 4) if total else 0.0)
    # ROI = (产出价值 − 成本) / 成本；产出价值按完成任务数 × 50¥（≈0.5h 人工基准）
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


async def snapshot_company(db: AsyncSession, company_id: str,
                           since: datetime | None = None) -> list[dict]:
    """对公司所有 Agent 计算并保存绩效快照。"""
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    results = []
    now = _now()
    for a in agents:
        m = await compute_agent_metrics(db, a.id, since)
        snap = PerformanceSnapshot(id=str(uuid.uuid4()), agent_id=a.id,
                                   company_id=company_id,
                                   period_start=since or datetime(2000, 1, 1),
                                   period_end=now, metrics=m, score=m["score"])
        db.add(snap)
        results.append({"agent_name": a.name, **m})
    await db.commit()
    return sorted(results, key=lambda r: -r["score"])
