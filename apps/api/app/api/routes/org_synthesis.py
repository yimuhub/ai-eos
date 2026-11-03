"""自动组织架构生成接口。

GET  /org-synthesis/readiness       资料覆盖度与缺口（只读）
POST /org-synthesis/analyze         从知识库生成组织蓝图（MANAGE）
GET  /org-synthesis/runs            历史分析
GET  /org-synthesis/runs/{run_id}   单次分析与草稿
POST /org-synthesis/runs/{run_id}/apply     落地部门与员工（MANAGE）
POST /org-synthesis/runs/{run_id}/rollback  按快照撤销（MANAGE）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, assert_company_owned,
                          get_current_user, get_db)
from app.core.permissions import Capability
from app.models.org_synthesis import OrgSynthesisRun
from app.services import org_synthesis as service

router = APIRouter(prefix="/org-synthesis", tags=["org-synthesis"])


class AnalyzeIn(BaseModel):
    company_id: str
    model_id: str | None = None
    instructions: str | None = Field(default=None, max_length=2000)


class ApplyAgentIn(BaseModel):
    name: str
    department: str
    headcount: int = 1


class ApplyIn(BaseModel):
    model_id: str | None = None
    agents: list[ApplyAgentIn] | None = None


async def _owned_run(db: AsyncSession, user_id: str, run_id: str) -> OrgSynthesisRun:
    run = await db.get(OrgSynthesisRun, run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    # 非成员与不存在同样返回 404，不泄露别家公司的分析是否存在。
    await assert_company_owned(db, user_id, run.company_id)
    return run


@router.get("/readiness")
async def readiness(company_id: str = Query(...),
                    db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    await assert_company_owned(db, user.id, company_id)
    return await service.compute_readiness(db, company_id)


@router.post("/analyze")
async def analyze(data: AnalyzeIn, db: AsyncSession = Depends(get_db),
                  user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    try:
        run = await service.run_synthesis(
            db, data.company_id, user.id,
            model_id=data.model_id, instructions=data.instructions)
    except service.SynthesisError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return service.serialize_run(run)


@router.get("/runs")
async def list_runs(company_id: str = Query(...), limit: int = Query(20, ge=1, le=100),
                    db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    await assert_company_owned(db, user.id, company_id)
    rows = (await db.execute(
        select(OrgSynthesisRun)
        .where(OrgSynthesisRun.company_id == company_id)
        .order_by(OrgSynthesisRun.created_at.desc())
        .limit(limit))).scalars().all()
    return [service.serialize_run(r) for r in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db),
                  user=Depends(get_current_user)):
    run = await _owned_run(db, user.id, run_id)
    return service.serialize_run(run)


@router.post("/runs/{run_id}/apply")
async def apply(run_id: str, data: ApplyIn | None = None,
                db: AsyncSession = Depends(get_db),
                user=Depends(get_current_user)):
    run = await _owned_run(db, user.id, run_id)
    await assert_company_capability(db, user.id, run.company_id,
                                    Capability.MANAGE)
    payload = data or ApplyIn()
    try:
        result = await service.apply_run(
            db, run, user.id,
            agents=[a.model_dump() for a in payload.agents]
            if payload.agents is not None else None,
            model_id=payload.model_id)
    except service.SynthesisError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return result


@router.post("/runs/{run_id}/rollback")
async def rollback(run_id: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    run = await _owned_run(db, user.id, run_id)
    await assert_company_capability(db, user.id, run.company_id,
                                    Capability.MANAGE)
    try:
        result = await service.rollback_run(db, run, user.id)
    except service.SynthesisError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return result
