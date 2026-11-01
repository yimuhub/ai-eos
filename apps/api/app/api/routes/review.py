"""Evaluation & Review API (P3-07)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.permissions import require_company_scope
from app.learning_engine import add_growth_event
from app.models.learning import GrowthEventType
from app.models.review import AgentReview, ReviewCycle, ReviewSource
from app.models.user import User
from app.schemas.responses import (AutoReviewOut, ReviewCycleOut,
                                   ReviewOut, ReviewSubmittedOut)

router = APIRouter(prefix="/reviews", tags=["reviews"],
                   dependencies=[Depends(require_company_scope())])
class ReviewIn(BaseModel):
    source: ReviewSource
    score: float                      # 0-100
    strengths: list[str] | None = None
    weaknesses: list[str] | None = None
    suggestions: list[str] | None = None
    comment: str | None = None


@router.post("/cycles", status_code=201,
             response_model=ReviewCycleOut)
async def create_cycle(
    name: str = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    c = ReviewCycle(id=str(__import__("uuid").uuid4()), company_id=company_id,
                    name=name)
    db.add(c)
    await db.commit()
    return {"id": c.id, "name": c.name}


@router.post("/agents/{agent_id}", status_code=201,
             response_model=ReviewSubmittedOut)
async def submit_review(
    agent_id: str,
    data: ReviewIn,
    reviewer_agent_id: str | None = Query(None),
    cycle_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 给别家员工打考核分会污染对方的档案与成长事件，必须校验归属。
    from app.api.deps import assert_agent_owned
    a = await assert_agent_owned(db, user.id, agent_id)
    if not 0 <= data.score <= 100:
        raise HTTPException(400, "score must be 0-100")

    evidence = None
    # AUTOMATED 考核必须附带真实指标（从 performance_engine 计算）
    if data.source == ReviewSource.AUTOMATED:
        from app.performance_engine import compute_agent_metrics
        metrics = await compute_agent_metrics(db, agent_id)
        if not metrics.get("tasks_total"):
            raise HTTPException(409, "无真实执行数据，禁止生成自动化评价")
        evidence = metrics
        reviewer = "system"
    else:
        reviewer = reviewer_agent_id or user.id

    review = AgentReview(
        id=str(__import__("uuid").uuid4()), agent_id=agent_id,
        company_id=a.company_id, source=data.source, score=data.score,
        strengths=data.strengths or [], weaknesses=data.weaknesses or [],
        suggestions=data.suggestions or [], evidence=evidence,
        comment=data.comment, reviewer=reviewer, cycle_id=cycle_id)
    db.add(review)
    await add_growth_event(db, agent_id, a.company_id,
                           GrowthEventType.EVALUATED,
                           f"{data.source.value} 考核：{data.score}分",
                           detail={"score": data.score},
                           source_type="review", actor=reviewer)
    await db.commit()
    return {"id": review.id, "score": review.score}


@router.get("/agents/{agent_id}", response_model=list[ReviewOut])
async def list_reviews(
    agent_id: str,
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 考核分与评语是员工隐私：路径里没有 company_id，router 级门禁会跳过，
    # 这里必须按 Agent 反查公司，否则知道 agent_id 就能读别家员工的考核。
    from app.api.deps import assert_agent_owned
    await assert_agent_owned(db, user.id, agent_id)
    q = await db.execute(select(AgentReview).where(
        AgentReview.agent_id == agent_id).order_by(
        AgentReview.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    return [{"id": r.id, "source": r.source.value, "score": r.score,
             "strengths": r.strengths, "weaknesses": r.weaknesses,
             "suggestions": r.suggestions, "comment": r.comment,
             "evidence": r.evidence,
             "created_at": r.created_at.isoformat()} for r in rows]


class AutoProposalIn(BaseModel):
    generate_training: bool = True


@router.post("/agents/{agent_id}/auto-review",
             response_model=AutoReviewOut)
async def auto_review(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """基于真实指标的自动化考核。分数来自 performance_engine，非模型编造。"""
    from app.performance_engine import compute_agent_metrics
    from app.api.deps import assert_agent_owned
    await assert_agent_owned(db, user.id, agent_id)
    m = await compute_agent_metrics(db, agent_id)
    if not m.get("tasks_total"):
        raise HTTPException(409, "无真实执行数据")
    return {
        "agent_id": agent_id,
        "automated_score": m["score"],
        "metrics": m,
        "strengths": ([f"任务完成率 {int((m['completion_rate'] or 0)*100)}%"]
                      if m.get("completion_rate") else []),
        "weaknesses": ([f"失败 {m['tasks_failed']} 次"] if m.get("tasks_failed") else []),
    }
