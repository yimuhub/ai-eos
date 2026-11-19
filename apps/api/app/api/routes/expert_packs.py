"""行业专家包与访谈 API（V6.2）。

目录公开（登录即可看）；解锁走超管门禁（/ops/admin 已有同款守卫的兄弟实现，
这里复用语义但单独实现以保证 ops.py 不膨胀）；访谈与提炼是公司内 OPERATE。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.logging_config import get_logger

logger = get_logger(__name__)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.interview import ExpertInterview
from app.models.user import User
from app.services import interview_service
from app.services.expert_pack_service import (get_expert_pack,
                                              list_expert_packs,
                                              seeded_expert_count,
                                              unlock_expert_pack)

router = APIRouter(prefix="/expert-packs", tags=["expert-packs"])
interviews_router = APIRouter(prefix="/interviews", tags=["expert-packs"])


async def _guard(db: AsyncSession, user: User, company_id: str,
                 cap: Capability = Capability.VIEW) -> None:
    await assert_company_capability(db, user.id, company_id, cap)


@router.get("")
async def catalog(company_id: str | None = Query(None),
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """内置专家包目录（按行业）；带 company_id 时附每包在本公司的解锁状态。"""
    if company_id:
        # company_id 是 Query 参数：必须校验调用者与该公司的归属，否则可枚举别家解锁数
        await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    packs = list_expert_packs()
    out = []
    for p in packs:
        seeded = None
        if company_id:
            seeded = await seeded_expert_count(db, company_id, p["id"])
        out.append({"id": p["id"], "name": p["name"], "version": p["version"],
                    "industry": p["industry"], "description": p["description"],
                    "categories": p.get("categories") or [],
                    "experts": [{"id": e["id"], "name": e["name"],
                                 "category": e.get("category"),
                                 "mission": e.get("mission"),
                                 "skills_count": len(e.get("skills") or [])}
                                for e in p.get("experts", [])],
                    "seeded_in_company": seeded})
    return {"packs": out}


@router.get("/{pack_id}")
async def pack_detail(pack_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    pack = get_expert_pack(pack_id)
    if pack is None:
        raise HTTPException(404, "专家包不存在")
    return {"pack": pack}


# ---------------------------------------------------------------- 超管解锁

class UnlockIn(BaseModel):
    pack_id: str
    company_id: str


@router.post("/admin/unlock")
async def admin_unlock(body: UnlockIn, request: Request,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """超管把内置专家包解锁到某公司（写入该公司 expert_templates）。"""
    from app.api.routes.ops import _admin_guard
    await _admin_guard(db, user, request)
    try:
        created = await unlock_expert_pack(db, body.pack_id,
                                           body.company_id, user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.services.event_system import log_event
    await log_event(db, "admin.expert_pack_unlocked", "company",
                    body.company_id, "user", user.id,
                    {"pack": body.pack_id, "created": created})
    await db.commit()
    return {"ok": True, "created": created,
            "message": f"专家包已解锁（新增 {created} 位专家到该公司专家库）"}


@interviews_router.get("/experts")
async def company_pack_experts(company_id: str = Query(...),
                               db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """本公司已解锁的行业专家模板（含访谈历史与技能数）——访谈 UI 的数据源。"""
    await _guard(db, user, company_id)
    from app.models.expert_template import ExpertTemplate
    rows = (await db.execute(
        select(ExpertTemplate).where(
            ExpertTemplate.company_id == company_id,
            ExpertTemplate.expert_pack_id.is_not(None))
        .order_by(ExpertTemplate.name))).scalars().all()
    return {"experts": [{
        "id": e.id, "name": e.name, "category": e.category,
        "pack_id": e.expert_pack_id,
        "department": e.department, "mission": e.description,
        "skills_count": len(e.skills or []),
        "interviews": len(e.interview_history or []),
        "skills": e.skills or [],
    } for e in rows]}


# ---------------------------------------------------------------- 访谈

class InterviewIn(BaseModel):
    transcript: str = Field(min_length=20)
    expert_template_id: str | None = None
    agent_id: str | None = None
    department: str | None = None
    interviewee: str | None = None
    audio_media_id: str | None = None


@interviews_router.post("", status_code=201)
async def create_interview(body: InterviewIn,
                           company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """登记一份部门访谈资料（文字或语音转写），等待总控提炼。"""
    await _guard(db, user, company_id, Capability.OPERATE)
    if body.expert_template_id is None and body.agent_id is None:
        raise HTTPException(400, "必须指定要完善的专家（expert_template_id 或 agent_id）")
    row = ExpertInterview(
        id=str(uuid.uuid4()), company_id=company_id,
        expert_template_id=body.expert_template_id,
        agent_id=body.agent_id, department=body.department,
        interviewee=body.interviewee, transcript=body.transcript,
        audio_media_id=body.audio_media_id, created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "interview_id": row.id, "status": row.status,
            "hint": "调 POST /interviews/{id}/digest 让总控提炼并应用到专家"}


@interviews_router.get("")
async def list_interviews(company_id: str = Query(...),
                          expert_template_id: str | None = Query(None),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    await _guard(db, user, company_id)
    q = select(ExpertInterview).where(
        ExpertInterview.company_id == company_id)
    if expert_template_id:
        q = q.where(ExpertInterview.expert_template_id == expert_template_id)
    rows = (await db.execute(
        q.order_by(ExpertInterview.created_at.desc()).limit(50))).scalars().all()
    return {"interviews": [{
        "id": r.id, "expert_template_id": r.expert_template_id,
        "agent_id": r.agent_id, "department": r.department,
        "interviewee": r.interviewee, "status": r.status,
        "digest": r.digest, "error": r.error,
        "transcript_head": (r.transcript or "")[:120],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


@interviews_router.post("/{interview_id}/digest")
async def digest_interview(interview_id: str, company_id: str = Query(...),
                           sync_agent: bool = Query(False),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """总控提炼：访谈资料 → 结构化技能/人设增强 → 应用到专家（可选同步 Agent）。"""
    await _guard(db, user, company_id, Capability.OPERATE)
    row = await db.get(ExpertInterview, interview_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "访谈记录不存在")

    from app.models.agent import Agent, AgentModelConfig
    from app.models.expert_template import ExpertTemplate

    expert = None
    agent = None
    if row.expert_template_id:
        expert = await db.get(ExpertTemplate, row.expert_template_id)
        if expert is None or expert.company_id != company_id:
            raise HTTPException(404, "目标专家模板不存在")
    if row.agent_id:
        agent = await db.get(Agent, row.agent_id)
        if agent is None or agent.company_id != company_id:
            raise HTTPException(404, "员工不存在")

    # 模型：优先目标 Agent 的配置；否则公司任一员工的配置（总控兜底）
    model_ids: list[str] = []
    target_agent_id = row.agent_id or row.expert_template_id and None
    if row.agent_id:
        cfgs = (await db.execute(
            select(AgentModelConfig).where(
                AgentModelConfig.agent_id == row.agent_id))).scalars().all()
        order = {"primary": 0, "secondary": 1, "fallback": 2}
        model_ids = [c.model_id for c in
                     sorted(cfgs, key=lambda c: order.get(c.role, 3))]
    if not model_ids:
        cfgs = (await db.execute(
            select(AgentModelConfig).where(
                AgentModelConfig.agent_id.in_(
                    select(Agent.id).where(Agent.company_id == company_id)))
            )).scalars().all()
        model_ids = [c.model_id for c in cfgs][:3]
    if not model_ids:
        raise HTTPException(400, "该公司还没有可用模型：请先在模型中心配置并绑定")

    from app.model_gateway import ModelGateway
    gateway = ModelGateway(db)

    async def gateway_fn(mid_list, messages):
        return await gateway.chat_with_agent_models(mid_list, messages)

    if expert is None and agent is not None:
        # 目标是已上岗 Agent：找它对应的公司专家模板（按名字回查），没有则
        # 现场补一份公司级模板（保证访谈留痕有载体）
        expert = (await db.execute(
            select(ExpertTemplate).where(
                ExpertTemplate.company_id == company_id,
                ExpertTemplate.name == agent.name))).scalars().first()
        if expert is None:
            expert = ExpertTemplate(
                id=str(uuid.uuid4()), company_id=company_id,
                source="interview", source_slug=f"agent:{agent.id}",
                name=agent.name, department=agent.role or "general",
                description=agent.description,
                system_prompt=agent.system_prompt or "",
                created_by=user.id)
            db.add(expert)
            await db.flush()
        row.expert_template_id = expert.id

    from app.services.event_system import log_event
    result = await interview_service.run_digest(db, row, expert, model_ids,
                                                gateway_fn)
    if result["status"] == "applied":
        stats = result.get("digest", {}).get("applied") or {}
        if sync_agent and agent is not None:
            agent.system_prompt = expert.system_prompt
            await db.commit()
        await log_event(db, "interview.digest_applied", "expert_template",
                        str(row.expert_template_id), "user", user.id,
                        {"interview_id": row.id, **stats},
                        company_id=company_id)
        await db.commit()
    return {"ok": result["status"] == "applied", **result}
