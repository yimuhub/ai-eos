"""CEO 自我进化接口：看时间线 / 手动触发 / 对月度提案拍板。

读端点只要公司归属；触发进化与拍板是写操作，要求 OPERATE。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.permissions import Capability, require_company_scope
from app.models.evolution import EvolutionRecord
from app.models.scheduler import AutomationSchedule
from app.self_evolution import (LEVELS, LEVEL_LABEL, active_playbook,
                                decide_action, ensure_evolution_schedules,
                                playbook_brief, record_brief, run_evolution)
from app.schemas.responses_evolution import (EvolutionDecideIn,
                                             EvolutionOverviewOut,
                                             EvolutionRecordOut,
                                             EvolutionRunIn,
                                             EvolutionSchedulesOut)

router = APIRouter(prefix="/evolution", tags=["evolution"],
                   dependencies=[Depends(require_company_scope())])


def _schedule_brief(row: AutomationSchedule) -> dict:
    return {
        "id": row.id, "name": row.name, "action_type": row.action_type,
        "trigger_type": row.trigger_type,
        "trigger_config": row.trigger_config or {},
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "run_count": int(row.run_count or 0),
    }


@router.get("", response_model=EvolutionOverviewOut)
async def overview(
    company_id: str = Query(...),
    level: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_owned(db, user.id, company_id)
    pb = await active_playbook(db, company_id)
    q = (select(EvolutionRecord)
         .where(EvolutionRecord.company_id == company_id)
         .order_by(EvolutionRecord.created_at.desc()).limit(limit))
    if level:
        q = q.where(EvolutionRecord.level == level)
    records = (await db.execute(q)).scalars().all()
    schedules = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == company_id,
            AutomationSchedule.action_type.like("ceo_evolve_%"),
            AutomationSchedule.status != "ARCHIVED")
        .order_by(AutomationSchedule.created_at))).scalars().all()
    return {
        "company_id": company_id,
        "playbook": playbook_brief(pb) if pb else None,
        "records": [record_brief(r) for r in records],
        "schedules": [_schedule_brief(s) for s in schedules],
        "levels": [{"level": lv.value, "label": LEVEL_LABEL[lv]}
                   for lv in LEVELS],
    }


@router.get("/{record_id}", response_model=EvolutionRecordOut)
async def detail(
    record_id: str,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    rec = await db.get(EvolutionRecord, record_id)
    if rec is None:
        raise HTTPException(404, "进化记录不存在")
    await deps.assert_company_owned(db, user.id, rec.company_id)
    return record_brief(rec)


@router.post("/run", response_model=EvolutionRecordOut)
async def run_now(
    data: EvolutionRunIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    try:
        return await run_evolution(db, data.company_id, data.level,
                                   user_id=user.id, source="manual")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{record_id}/actions/{action_id}", response_model=EvolutionRecordOut)
async def decide(
    record_id: str,
    action_id: str,
    data: EvolutionDecideIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    rec = await db.get(EvolutionRecord, record_id)
    if rec is None:
        raise HTTPException(404, "进化记录不存在")
    await deps.assert_company_capability(db, user.id, rec.company_id,
                                         Capability.OPERATE)
    try:
        return await decide_action(db, rec, action_id, approve=data.approve,
                                   user_id=user.id, note=data.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/schedules", response_model=EvolutionSchedulesOut)
async def ensure_schedules(
    data: EvolutionRunIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    created = await ensure_evolution_schedules(db, data.company_id, user.id)
    schedules = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == data.company_id,
            AutomationSchedule.action_type.like("ceo_evolve_%"),
            AutomationSchedule.status != "ARCHIVED")
        .order_by(AutomationSchedule.created_at))).scalars().all()
    return {"company_id": data.company_id,
            "schedules": [_schedule_brief(s) for s in schedules],
            "created": created}
