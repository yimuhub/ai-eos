"""Performance API (P2-11)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.performance import PerformanceSnapshot
from app.performance_engine import compute_agent_metrics, snapshot_company
from app.models.user import User

router = APIRouter(prefix="/performance", tags=["performance"])


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
