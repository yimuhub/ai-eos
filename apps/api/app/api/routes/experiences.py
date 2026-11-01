"""Agent 经验池接口：浏览、手动提炼、把同事经验学进自己的记忆。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.permissions import Capability, require_company_scope
from app.experience_engine import (adopt_experience, experience_brief,
                                   harvest_task_experience)
from app.models.experience import SharedExperience
from app.models.task import Task, TaskStatus
from app.schemas.responses_experience import (ExperienceAdoptIn,
                                              ExperienceHarvestIn,
                                              ExperienceOut,
                                              ExperienceOverviewOut)

router = APIRouter(prefix="/experiences", tags=["experiences"],
                   dependencies=[Depends(require_company_scope())])


@router.get("", response_model=ExperienceOverviewOut)
async def overview(
    company_id: str = Query(...),
    agent_id: str | None = Query(None),
    kind: str | None = Query(None),
    reuse_key: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    from app.models.agent import Agent

    await deps.assert_company_owned(db, user.id, company_id)
    q = (select(SharedExperience)
         .where(SharedExperience.company_id == company_id,
                SharedExperience.status != "retired"))
    if agent_id:
        q = q.where(SharedExperience.agent_id == agent_id)
    if kind:
        q = q.where(SharedExperience.kind == kind)
    if reuse_key:
        q = q.where(SharedExperience.reuse_key == reuse_key)
    rows = (await db.execute(
        q.order_by(SharedExperience.created_at.desc()).limit(limit)
    )).scalars().all()

    stat_rows = (await db.execute(
        select(SharedExperience.kind, SharedExperience.adopted_by,
               SharedExperience.tokens_saved_est)
        .where(SharedExperience.company_id == company_id,
               SharedExperience.status != "retired"))).all()
    total = len(stat_rows)
    success = sum(1 for r in stat_rows if r[0] == "success")
    failure = sum(1 for r in stat_rows if r[0] == "failure")
    adopted = sum(len(r[1] or []) for r in stat_rows)
    saved = sum(int(r[2] or 0) for r in stat_rows)

    items = [experience_brief(r) for r in rows]
    if items:
        author_ids = {r.agent_id for r in rows}
        names = dict((await db.execute(
            select(Agent.id, Agent.name)
            .where(Agent.id.in_(author_ids)))).all())
        for item, row in zip(items, rows):
            item["author_agent_name"] = names.get(row.agent_id)
    return {
        "company_id": company_id,
        "items": items,
        "stats": {"total": int(total or 0), "success": int(success or 0),
                  "failure": int(failure or 0), "adopted": int(adopted or 0),
                  "tokens_saved_est": int(saved or 0)},
    }


@router.get("/{experience_id}", response_model=ExperienceOut)
async def detail(
    experience_id: str,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    row = await db.get(SharedExperience, experience_id)
    if row is None:
        raise HTTPException(404, "经验不存在")
    await deps.assert_company_owned(db, user.id, row.company_id)
    return experience_brief(row)


@router.post("/harvest", response_model=ExperienceOut | None)
async def harvest(
    data: ExperienceHarvestIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    task = await db.get(Task, data.task_id)
    if task is None or task.company_id != data.company_id:
        raise HTTPException(404, "任务不存在或不属于这家公司")
    if not task.assigned_agent_id:
        raise HTTPException(400, "任务没有负责人，无法提炼经验")
    from app.models.agent import Agent
    agent = await db.get(Agent, task.assigned_agent_id)
    if agent is None:
        raise HTTPException(404, "任务负责人已不存在")
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                           TaskStatus.CANCELLED):
        raise HTTPException(400, "任务尚未结束，不能提炼经验")
    return await harvest_task_experience(
        db, task, agent, success=task.status == TaskStatus.COMPLETED,
        source="manual")


@router.post("/{experience_id}/adopt", response_model=ExperienceOut)
async def adopt(
    experience_id: str,
    data: ExperienceAdoptIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    row = await db.get(SharedExperience, experience_id)
    if row is None:
        raise HTTPException(404, "经验不存在")
    await deps.assert_company_capability(db, user.id, row.company_id,
                                         Capability.OPERATE)
    try:
        return await adopt_experience(db, row, data.agent_id, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
