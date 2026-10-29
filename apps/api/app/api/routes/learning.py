"""Learning Center API (P3-01)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.learning_engine import (add_growth_event, ensure_profile,
                                 get_full_profile, update_profile)
from app.models.agent import Agent
from app.models.learning import (AgentLearningRecord, GrowthEventType)
from app.models.user import User

router = APIRouter(prefix="/learning", tags=["learning"])


async def _agent(db: AsyncSession, agent_id: str, user_id: str) -> Agent:
    """取 Agent 并校验归属：成长档案属员工私有数据，不能凭 id 读改别家员工。"""
    from app.api.deps import assert_agent_owned

    return await assert_agent_owned(db, user_id, agent_id)


@router.get("/agents/{agent_id}/profile")
async def profile(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 完整成长档案（自动建档）。"""
    a = await _agent(db, agent_id, user.id)
    return await get_full_profile(db, a)


class ProfileUpdate(BaseModel):
    identity: str | None = None
    capabilities: list[str] | None = None
    preferences: dict | None = None
    learning_goals: list[str] | None = None


@router.patch("/agents/{agent_id}/profile")
async def patch_profile(
    agent_id: str,
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新档案（自动记 Growth Event + 能力版本化）。"""
    a = await _agent(db, agent_id, user.id)
    profile = await ensure_profile(db, a, a.company_id)

    changes = {}
    event_map = []
    if data.identity is not None and data.identity != profile.identity:
        changes["identity"] = data.identity
        event_map.append(GrowthEventType.CAPABILITY_CHANGED)
    if data.capabilities is not None:
        changes["capabilities"] = data.capabilities
        event_map.append(GrowthEventType.CAPABILITY_CHANGED)
    if data.preferences is not None:
        merged = dict(profile.preferences or {})
        merged.update(data.preferences)
        changes["preferences"] = merged
        event_map.append(GrowthEventType.CAPABILITY_CHANGED)
    if data.learning_goals is not None:
        changes["learning_goals"] = data.learning_goals
    if not changes:
        return {"changed": False}

    et = event_map[0] if event_map else GrowthEventType.CAPABILITY_CHANGED
    await update_profile(db, profile, changes, et,
                         title=f"档案更新：{', '.join(changes.keys())}",
                         source_type="manual", actor=user.id)
    return {"changed": True, "version": profile.profile_version}


# ---------- 学习计划 ----------

class LearningGoalIn(BaseModel):
    goal: str
    plan: dict | None = None


@router.post("/agents/{agent_id}/learning-records", status_code=201)
async def create_learning_record(
    agent_id: str,
    data: LearningGoalIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    a = await _agent(db, agent_id, user.id)
    rec = AgentLearningRecord(id=str(uuid.uuid4()), agent_id=a.id,
                              goal=data.goal, plan=data.plan or {})
    db.add(rec)
    await add_growth_event(db, a.id, a.company_id,
                           GrowthEventType.TRAINED,
                           f"新学习目标：{data.goal[:50]}",
                           source_type="manual", actor=user.id)
    await db.commit()
    return {"id": rec.id, "status": rec.status}


class ProgressIn(BaseModel):
    progress: int
    status: str | None = None


@router.patch("/learning-records/{record_id}")
async def update_learning_record(
    record_id: str,
    data: ProgressIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rec = await db.get(AgentLearningRecord, record_id)
    if not rec:
        raise HTTPException(404, "not found")
    if not 0 <= data.progress <= 100:
        raise HTTPException(400, "progress 0-100")
    rec.progress = data.progress
    if data.status:
        rec.status = data.status
    elif rec.progress >= 100:
        rec.status = "DONE"
    await db.commit()
    return {"id": rec.id, "progress": rec.progress, "status": rec.status}


@router.get("/agents/{agent_id}/growth-events")
async def growth_events(
    agent_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from app.models.learning import AgentGrowthEvent
    q = await db.execute(select(AgentGrowthEvent).where(
        AgentGrowthEvent.agent_id == agent_id).order_by(
        AgentGrowthEvent.created_at.desc()).limit(limit))
    return [{"id": e.id, "type": e.event_type.value, "title": e.title,
             "detail": e.detail, "source_type": e.source_type,
             "created_at": e.created_at.isoformat()} for e in q.scalars().all()]
