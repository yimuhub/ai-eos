"""Performance API (P2-11)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.performance import PerformanceSnapshot
from app.performance_engine import compute_agent_metrics, snapshot_company
from app.models.user import User

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/company/{company_id}/hr-overview")
async def hr_overview(company_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """V5.5 C1：公司 HR 看板——编制 / 成本 / 产出 / 考核分 四象限。"""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.token_usage import TokenUsage
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, company_id)

    # 1) 编制：全部员工 + 状态分布
    agents = (await db.execute(
        select(Agent).where(Agent.company_id == company_id))).scalars().all()
    status_dist: dict[str, int] = {}
    for a in agents:
        status_dist[a.status.value] = status_dist.get(a.status.value, 0) + 1

    # 2) 成本：本月 token 用量（按 Agent 汇总 top3）
    month_start = datetime.now().replace(day=1, hour=0, minute=0,
                                         second=0, microsecond=0)
    month_total_row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0)).where(
            TokenUsage.company_id == company_id,
            TokenUsage.created_at >= month_start))).one()
    top_cost_rows = (await db.execute(
        select(TokenUsage.agent_id,
               func.sum(TokenUsage.total_tokens),
               func.sum(TokenUsage.total_cost))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.created_at >= month_start,
               TokenUsage.agent_id.is_not(None))
        .group_by(TokenUsage.agent_id).order_by(func.sum(TokenUsage.total_tokens).desc())
        .limit(3))).all()
    amap = {a.id: a.name for a in agents}
    top_cost = [{"agent": amap.get(r[0], r[0]),
                 "tokens": int(r[1] or 0), "cost": round(float(r[2] or 0), 4)}
                for r in top_cost_rows]

    # 3) 产出：近 30 天完成任务（按负责人 top3）
    since30 = datetime.now() - timedelta(days=30)
    done_rows = (await db.execute(
        select(Task.assigned_agent_id, func.count()).where(
            Task.company_id == company_id,
            Task.status == "COMPLETED",
            Task.updated_at >= since30,
            Task.assigned_agent_id.is_not(None))
        .group_by(Task.assigned_agent_id).order_by(func.count().desc())
        .limit(3))).all()
    top_output = [{"agent": amap.get(r[0], r[0]), "tasks": int(r[1])}
                  for r in done_rows]
    total_done_30 = int((await db.execute(
        select(func.count()).where(Task.company_id == company_id,
                                   Task.status == "COMPLETED",
                                   Task.updated_at >= since30))).scalar() or 0)

    # 4) 考核分：最近一次公司快照排行榜 top3（无快照则实时算前 3 个）
    ranked: list[dict] = []
    snaps = (await db.execute(
        select(PerformanceSnapshot).where(
            PerformanceSnapshot.company_id == company_id)
        .order_by(PerformanceSnapshot.created_at.desc())
        .limit(max(len(agents) * 3, 9)))).scalars().all()
    seen: set[str] = set()
    # 按"同一 agent 最新一条"去重取排行榜
    for s in snaps:
        if s.agent_id in seen:
            continue
        seen.add(s.agent_id)
        ranked.append({"agent": amap.get(s.agent_id, s.agent_id),
                       "score": s.score})
        if len(ranked) >= 5:
            break
    top_score = sorted(ranked, key=lambda x: -x["score"])[:3]

    # 公司健康度：有编制+有产出+成本在控（简化公式，给老板一个数）
    healthy = 1.0
    if total_done_30 == 0:
        healthy = min(healthy, 0.5)
    if not agents:
        healthy = 0.2
    return {
        "company": {"id": company_id},
        "headcount": {"total": len(agents), "by_status": status_dist},
        "cost_month": {"tokens": int(month_total_row[0] or 0),
                       "cost": round(float(month_total_row[1] or 0), 2),
                       "top": top_cost},
        "output_30d": {"total_tasks": int(total_done_30), "top": top_output},
        "assessment_top": top_score,
        "health": round(healthy, 2),
    }


@router.get("/agents/{agent_id}")
async def agent_performance(
    agent_id: str,
    since: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单 Agent 实时绩效（从真实执行数据计算）。"""
    from datetime import datetime
    since_dt = datetime.fromisoformat(since) if since else None
    metrics = await compute_agent_metrics(db, agent_id, since_dt)
    return metrics


@router.post("/company/{company_id}/snapshot")
async def take_snapshot(
    company_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对公司全体 Agent 做绩效快照并返回排行榜。"""
    results = await snapshot_company(db, company_id)
    return {"ranked": results}


@router.get("/company/{company_id}/history")
async def snapshot_history(
    company_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(PerformanceSnapshot).where(
        PerformanceSnapshot.company_id == company_id).order_by(
        PerformanceSnapshot.created_at.desc()).limit(limit))
    snaps = q.scalars().all()
    return [{"id": s.id, "agent_id": s.agent_id, "score": s.score,
             "metrics": s.metrics, "period_end": s.period_end.isoformat()}
            for s in snaps]
