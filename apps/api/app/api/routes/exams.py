"""CEO 周考接口：看考卷 / 手动开考 / 老板覆盖评分。

读端点只要公司归属；开考与覆盖评分是写操作，要求 OPERATE。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.ceo_engine import CeoError
from app.core.permissions import Capability, require_company_scope
from app.exam_engine import (DIMENSIONS, SUPERVISOR_NAME, ensure_exam_schedules,
                             exam_brief, override_exam, run_exam)
from app.models.agent import Agent
from app.models.exam import CeoExam
from app.models.scheduler import AutomationSchedule
from app.schemas.responses_exam import (ExamOut, ExamOverviewOut,
                                        ExamOverrideIn, ExamRunIn,
                                        ExamSchedulesOut)

router = APIRouter(prefix="/exams", tags=["exams"],
                   dependencies=[Depends(require_company_scope())])


def _schedule_brief(row: AutomationSchedule) -> dict:
    return {
        "id": row.id, "name": row.name, "action_type": row.action_type,
        "trigger_type": row.trigger_type,
        "trigger_config": row.trigger_config or {},
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "run_count": int(row.run_count or 0),
    }


def _supervisor_brief(row: Agent | None) -> dict | None:
    if row is None:
        return None
    return {"id": row.id, "name": row.name, "role": row.role,
            "status": getattr(row.status, "value", row.status), "created_at":
            row.created_at.isoformat() if row.created_at else None}


@router.get("", response_model=ExamOverviewOut)
async def overview(
    company_id: str = Query(...),
    limit: int = Query(12, ge=1, le=52),
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_owned(db, user.id, company_id)
    rows = (await db.execute(
        select(CeoExam).where(CeoExam.company_id == company_id)
        .order_by(CeoExam.created_at.desc()).limit(limit))).scalars().all()
    supervisor = (await db.execute(
        select(Agent).where(Agent.company_id == company_id,
                            Agent.name == SUPERVISOR_NAME)
        .limit(1))).scalars().first()
    sched = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == company_id,
            AutomationSchedule.action_type == "ceo_exam_weekly",
            AutomationSchedule.status != "ARCHIVED")
        .limit(1))).scalars().first()
    return {
        "company_id": company_id,
        "latest": exam_brief(rows[0]) if rows else None,
        "exams": [exam_brief(r) for r in rows],
        "supervisor": _supervisor_brief(supervisor),
        "schedule": _schedule_brief(sched) if sched else None,
        "dimensions": [{"key": k, "label": v} for k, v in DIMENSIONS.items()],
    }


@router.get("/{exam_id}", response_model=ExamOut)
async def detail(
    exam_id: str,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    row = await db.get(CeoExam, exam_id)
    if row is None:
        raise HTTPException(404, "考试记录不存在")
    await deps.assert_company_owned(db, user.id, row.company_id)
    return exam_brief(row)


@router.post("/run", response_model=ExamOut)
async def run_now(
    data: ExamRunIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    try:
        return await run_exam(db, data.company_id, user_id=user.id,
                              source="manual")
    except CeoError as exc:
        raise HTTPException(exc.status_code, exc.message)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{exam_id}/override", response_model=ExamOut)
async def override(
    exam_id: str,
    data: ExamOverrideIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    row = await db.get(CeoExam, exam_id)
    if row is None:
        raise HTTPException(404, "考试记录不存在")
    await deps.assert_company_capability(db, user.id, row.company_id,
                                         Capability.OPERATE)
    try:
        return await override_exam(
            db, row, user_id=user.id,
            grades=[g.model_dump() for g in data.grades] if data.grades else None,
            personality=data.personality, summary=data.summary,
            overall=data.overall, note=data.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/schedules", response_model=ExamSchedulesOut)
async def ensure_schedules(
    data: ExamRunIn,
    db: AsyncSession = Depends(deps.get_db),
    user=Depends(deps.get_current_user),
):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    created = await ensure_exam_schedules(db, data.company_id, user.id)
    rows = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == data.company_id,
            AutomationSchedule.action_type == "ceo_exam_weekly",
            AutomationSchedule.status != "ARCHIVED")
        .order_by(AutomationSchedule.created_at))).scalars().all()
    return {"company_id": data.company_id,
            "schedules": [_schedule_brief(s) for s in rows],
            "created": created}
