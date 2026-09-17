"""Learning Center engine (P3-01): profile lifecycle + growth events."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learning import (AgentCapabilityHistory, AgentGrowthEvent,
                                 AgentLearningRecord, AgentProfile,
                                 GrowthEventType)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def ensure_profile(db: AsyncSession, agent, company_id: str) -> AgentProfile:
    """获取或创建 Agent 成长档案（首次访问时自动建档）。"""
    q = await db.execute(select(AgentProfile).where(
        AgentProfile.agent_id == agent.id))
    profile = q.scalars().first()
    if profile:
        return profile
    profile = AgentProfile(
        id=str(uuid.uuid4()), agent_id=agent.id, company_id=company_id,
        identity=agent.role or agent.name,
        capabilities=[], preferences={}, learning_goals=[])
    db.add(profile)
    await db.flush()
    # 建档本身也是成长事件
    await add_growth_event(
        db, agent.id, company_id, GrowthEventType.CREATED,
        f"Agent 建档：{agent.name}", detail={"identity": profile.identity},
        source_type="system")
    await db.commit()
    return profile


async def add_growth_event(db: AsyncSession, agent_id: str, company_id: str,
                           event_type: GrowthEventType, title: str,
                           detail: dict | None = None,
                           source_type: str | None = None,
                           source_id: str | None = None,
                           actor: str | None = None) -> AgentGrowthEvent:
    """记录一条成长事件（append-only）。"""
    ev = AgentGrowthEvent(
        id=str(uuid.uuid4()), agent_id=agent_id, company_id=company_id,
        event_type=event_type, title=title, detail=detail or {},
        source_type=source_type, source_id=source_id, actor=actor)
    db.add(ev)
    return ev


async def update_profile(db: AsyncSession, profile: AgentProfile,
                         changes: dict, event_type: GrowthEventType,
                         title: str, source_type: str | None = None,
                         source_id: str | None = None,
                         actor: str | None = None) -> AgentProfile:
    """更新档案并自动：记事件 + 能力历史版本化 + 版本号自增。"""
    before = {k: getattr(profile, k) for k in changes}
    for k, v in changes.items():
        setattr(profile, k, v)
    profile.profile_version += 1
    await db.flush()

    await add_growth_event(db, profile.agent_id, profile.company_id,
                           event_type, title,
                           detail={"before": _jsonable(before),
                                   "after": _jsonable(changes)},
                           source_type=source_type, source_id=source_id,
                           actor=actor)
    if "capabilities" in changes:
        max_q = await db.execute(select(func.max(AgentCapabilityHistory.version))
                                 .where(AgentCapabilityHistory.agent_id ==
                                        profile.agent_id))
        ver = (max_q.scalar() or 0) + 1
        db.add(AgentCapabilityHistory(
            id=str(uuid.uuid4()), agent_id=profile.agent_id, version=ver,
            capabilities=changes["capabilities"],
            snapshot_reason=title))
    await db.commit()
    await db.refresh(profile)
    return profile


def _jsonable(v):
    import json as _j
    try:
        _j.dumps(v)
        return v
    except Exception:
        return str(v)


async def get_full_profile(db: AsyncSession, agent) -> dict:
    """聚合完整成长档案（Learning Center 页面数据）。"""
    from app.models.learning import AgentCapabilityHistory
    from app.models.meeting import MeetingParticipant
    from app.models.task import AgentExecution

    company_id = agent.company_id
    profile = await ensure_profile(db, agent, company_id)

    exec_q = await db.execute(select(AgentExecution).where(
        AgentExecution.agent_id == agent.id).order_by(
        AgentExecution.created_at.desc()).limit(50))
    executions = exec_q.scalars().all()

    part_q = await db.execute(select(MeetingParticipant).where(
        MeetingParticipant.agent_id == agent.id))
    spoke = sum(p.spoke_count or 0 for p in part_q.scalars().all())

    cap_q = await db.execute(select(AgentCapabilityHistory).where(
        AgentCapabilityHistory.agent_id == agent.id).order_by(
        AgentCapabilityHistory.version.desc()).limit(10))

    ev_q = await db.execute(select(AgentGrowthEvent).where(
        AgentGrowthEvent.agent_id == agent.id).order_by(
        AgentGrowthEvent.created_at.desc()).limit(100))

    lr_q = await db.execute(select(AgentLearningRecord).where(
        AgentLearningRecord.agent_id == agent.id).order_by(
        AgentLearningRecord.created_at.desc()))

    completed = sum(1 for e in executions
                    if (e.status or "").upper() in ("COMPLETED", "SUCCESS", "DONE"))
    failed = sum(1 for e in executions
                 if (e.status or "").upper() in ("FAILED", "ERROR"))

    return {
        "profile": {
            "id": profile.id, "identity": profile.identity,
            "capabilities": profile.capabilities or [],
            "preferences": profile.preferences or {},
            "learning_goals": profile.learning_goals or [],
            "version": profile.profile_version,
        },
        "stats": {
            "tasks_total": len(executions),
            "tasks_completed": completed,
            "tasks_failed": failed,
            "meeting_spoke_count": spoke,
        },
        "capability_history": [
            {"version": c.version, "capabilities": c.capabilities,
             "reason": c.snapshot_reason,
             "created_at": c.created_at.isoformat()} for c in cap_q.scalars().all()],
        "growth_events": [
            {"type": e.event_type.value, "title": e.title, "detail": e.detail,
             "source_type": e.source_type,
             "created_at": e.created_at.isoformat()} for e in ev_q.scalars().all()],
        "learning_records": [
            {"id": r.id, "goal": r.goal, "status": r.status,
             "progress": r.progress} for r in lr_q.scalars().all()],
    }
