"""超级 Agent（CEO）指挥台接口。

老板只面对这一个入口：一句话 → 拆解预览 → 开工 → 盯进度 → 收报告。
写操作走 OPERATE 能力；指定 CEO（改动公司级配置）走 ADMIN。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.ceo_engine import (CeoError, append_instruction, build_digest,
                            cancel_mission, create_mission, ensure_digest_schedule,
                            launch_mission, mission_detail, mission_list_item,
                            pause_mission, push_digest, reclaim_mission,
                            resume_mission, set_ceo_agent)
from app.core.permissions import Capability
from app.core.logging_config import get_logger
from app.db.session import get_db
from app.models.agent import Agent
from app.models.ceo import CeoMission
from app.models.task import Task
from app.schemas.responses_ceo import (CeoAssignOut, CeoCancelOut,
                                       CeoCapacityOut, CeoConfigOut,
                                       CeoDigestOut, CeoDigestScheduleOut,
                                       CeoInstructionOut, CeoLaunchOut,
                                       CeoMissionDetailOut, CeoMissionListItemOut,
                                       CeoMissionPreviewOut, CeoReportOut,
                                       CeoRetryOut, CeoStatusOut)
from app.services.ceo_concurrency import current_capacity

router = APIRouter(prefix="/ceo", tags=["ceo"])
logger = get_logger(__name__)


class CreateMissionIn(BaseModel):
    company_id: str
    instruction: str = Field(min_length=1, max_length=4000)
    title: str | None = None
    coordinator_agent_id: str | None = None
    max_parallel: int | None = Field(default=None, ge=2, le=10)


class LaunchIn(BaseModel):
    max_parallel: int | None = Field(default=None, ge=2, le=10)


class AssignCeoIn(BaseModel):
    company_id: str
    agent_id: str


class InstructionIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class DigestScheduleIn(BaseModel):
    company_id: str
    time: str = "09:00"


def _raise(exc: CeoError) -> None:
    raise HTTPException(exc.status_code, exc.message)


async def _mission_or_404(db: AsyncSession, user_id: str,
                          mission_id: str, capability=None) -> CeoMission:
    """读端点只要归属；写端点再要求 OPERATE，避免只读成员能开工/暂停。"""
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise HTTPException(404, "任务不存在")
    if capability is None:
        await deps.assert_company_owned(db, user_id, mission.company_id)
    else:
        await deps.assert_company_capability(db, user_id, mission.company_id,
                                             capability)
    return mission


# ---------------------------------------------------------------------------
# CEO 指派
# ---------------------------------------------------------------------------

@router.get("/config", response_model=CeoConfigOut)
async def get_ceo_config(company_id: str = Query(...),
                         db: AsyncSession = Depends(get_db),
                         user=Depends(deps.get_current_user)):
    await deps.assert_company_owned(db, user.id, company_id)
    agents = (await db.execute(
        select(Agent).where(Agent.company_id == company_id)
        .order_by(Agent.created_at))).scalars().all()
    ceo = next((a for a in agents if a.is_ceo), None)
    if ceo is None:
        hints = ("ceo", "总经理", "总裁", "首席执行官", "负责人")
        ceo = next(
            (a for a in agents
             if any(h in f"{a.name or ''} {a.role or ''}".lower() for h in hints)),
            None)
    return {
        "company_id": company_id,
        "has_ceo": ceo is not None,
        "ceo_agent_id": str(ceo.id) if ceo else "",
        "ceo_agent_name": ceo.name if ceo else "",
        "candidates": [{"id": str(a.id), "name": a.name, "role": a.role}
                       for a in agents],
    }


@router.post("/config", response_model=CeoAssignOut)
async def assign_ceo(data: AssignCeoIn,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.ADMIN)
    try:
        result = await set_ceo_agent(db, data.company_id, data.agent_id)
    except CeoError as exc:
        _raise(exc)
    # 指定 CEO 即开通每日 09:00 运行日报（幂等）。超级 Agent 的第一职责
    # 是替老板盯盘，日报不该再要求老板自己去日程里手动建一条。
    await ensure_digest_schedule(db, data.company_id, user.id)
    # 同时开通日/周/月三级自我进化（幂等）：CEO 的成长节奏与日报同源，
    # 不需要老板再去日程里配三条。
    try:
        from app.self_evolution import ensure_evolution_schedules
        await ensure_evolution_schedules(db, data.company_id, user.id)
    except Exception as exc:  # noqa: BLE001 — 进化调度失败不能挡住指派 CEO
        logger.warning("自我进化调度创建失败 company=%s: %s",
                       data.company_id, exc)
    return result


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

@router.post("/missions", response_model=CeoMissionPreviewOut, status_code=201)
async def create(data: CreateMissionIn,
                 db: AsyncSession = Depends(get_db),
                 user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    try:
        return await create_mission(
            db, company_id=data.company_id, user_id=user.id,
            instruction=data.instruction, title=data.title,
            coordinator_agent_id=data.coordinator_agent_id,
            max_parallel=data.max_parallel)
    except CeoError as exc:
        _raise(exc)


@router.post("/missions/{mission_id}/launch", response_model=CeoLaunchOut,
             status_code=202)
async def launch(mission_id: str, data: LaunchIn,
                 db: AsyncSession = Depends(get_db),
                 user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await launch_mission(db, mission_id, data.max_parallel)
    except CeoError as exc:
        _raise(exc)


@router.get("/missions", response_model=list[CeoMissionListItemOut])
async def list_missions(company_id: str = Query(...),
                        status: str | None = Query(default=None),
                        limit: int = Query(default=30, ge=1, le=100),
                        db: AsyncSession = Depends(get_db),
                        user=Depends(deps.get_current_user)):
    await deps.assert_company_owned(db, user.id, company_id)
    stmt = select(CeoMission).where(CeoMission.company_id == company_id)
    if status:
        stmt = stmt.where(CeoMission.status == status)
    rows = (await db.execute(
        stmt.order_by(CeoMission.created_at.desc()).limit(limit))).scalars().all()
    agent_ids = {m.coordinator_agent_id for m in rows if m.coordinator_agent_id}
    name_map: dict[str, str] = {}
    if agent_ids:
        agent_rows = (await db.execute(
            select(Agent.id, Agent.name)
            .where(Agent.id.in_(agent_ids)))).all()
        name_map = {r[0]: r[1] for r in agent_rows}
    return [mission_list_item(m, name_map.get(m.coordinator_agent_id, ""))
            for m in rows]


@router.get("/missions/{mission_id}", response_model=CeoMissionDetailOut)
async def get_mission(mission_id: str,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(deps.get_current_user)):
    mission = await _mission_or_404(db, user.id, mission_id)
    tasks = (await db.execute(
        select(Task).where(Task.mission_id == mission.id)
        .order_by(Task.created_at))).scalars().all()
    agent_ids = {t.assigned_agent_id for t in tasks if t.assigned_agent_id}
    if mission.coordinator_agent_id:
        agent_ids.add(mission.coordinator_agent_id)
    agents: dict[str, Agent] = {}
    if agent_ids:
        rows = (await db.execute(
            select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()
        agents = {str(a.id): a for a in rows}
    return mission_detail(mission, tasks, agents)


@router.get("/missions/{mission_id}/report", response_model=CeoReportOut)
async def get_report(mission_id: str,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(deps.get_current_user)):
    mission = await _mission_or_404(db, user.id, mission_id)
    return {
        "mission_id": mission.id,
        "title": mission.title,
        "status": mission.status,
        "report": mission.report or "",
        "artifact_id": mission.report_artifact_id,
        "finished_at": mission.finished_at or "",
        "stats": mission.stats or {},
    }


@router.post("/missions/{mission_id}/pause", response_model=CeoStatusOut)
async def pause(mission_id: str,
                db: AsyncSession = Depends(get_db),
                user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await pause_mission(db, mission_id)
    except CeoError as exc:
        _raise(exc)


@router.post("/missions/{mission_id}/resume", response_model=CeoStatusOut)
async def resume(mission_id: str,
                 db: AsyncSession = Depends(get_db),
                 user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await resume_mission(db, mission_id)
    except CeoError as exc:
        _raise(exc)


@router.post("/missions/{mission_id}/cancel", response_model=CeoCancelOut)
async def cancel(mission_id: str,
                 db: AsyncSession = Depends(get_db),
                 user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await cancel_mission(db, mission_id)
    except CeoError as exc:
        _raise(exc)


@router.post("/missions/{mission_id}/reclaim", response_model=CeoRetryOut,
             status_code=202)
async def reclaim(mission_id: str,
                  db: AsyncSession = Depends(get_db),
                  user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await reclaim_mission(db, mission_id)
    except CeoError as exc:
        _raise(exc)


@router.post("/missions/{mission_id}/instruction", response_model=CeoInstructionOut)
async def instruct(mission_id: str, data: InstructionIn,
                   db: AsyncSession = Depends(get_db),
                   user=Depends(deps.get_current_user)):
    await _mission_or_404(db, user.id, mission_id, Capability.OPERATE)
    try:
        return await append_instruction(db, mission_id, data.text)
    except CeoError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# 容量 / 日报
# ---------------------------------------------------------------------------

@router.get("/capacity", response_model=CeoCapacityOut)
async def capacity(company_id: str = Query(...),
                   requested: int | None = Query(default=None, ge=2, le=10),
                   coordinator_agent_id: str | None = Query(default=None),
                   db: AsyncSession = Depends(get_db),
                   user=Depends(deps.get_current_user)):
    await deps.assert_company_owned(db, user.id, company_id)
    return await current_capacity(db, company_id, coordinator_agent_id, requested)


@router.post("/digest/run", response_model=CeoDigestOut)
async def run_digest(company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, company_id,
                                         Capability.OPERATE)
    return await push_digest(db, company_id)


@router.get("/digest", response_model=CeoDigestOut)
async def get_digest(company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user=Depends(deps.get_current_user)):
    await deps.assert_company_owned(db, user.id, company_id)
    return await build_digest(db, company_id)


@router.post("/digest/schedule", response_model=CeoDigestScheduleOut)
async def schedule_digest(data: DigestScheduleIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    if not (len(data.time) == 5 and data.time[2] == ":"
            and data.time[:2].isdigit() and data.time[3:].isdigit()
            and 0 <= int(data.time[:2]) <= 23 and 0 <= int(data.time[3:]) <= 59):
        raise HTTPException(400, "时间格式应为 HH:MM")
    return await ensure_digest_schedule(db, data.company_id, user.id, data.time)
