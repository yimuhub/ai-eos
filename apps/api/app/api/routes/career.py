"""Career & Promotion API (P3-05 + P3-06)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.learning_engine import add_growth_event
from app.models.agent import Agent
from app.models.career import (AgentCareerRecord, CareerPosition,
                               CareerProposal, TrialAssignment)
from app.models.learning import GrowthEventType
from app.models.skill_growth import AgentSkillNode
from app.models.user import User

router = APIRouter(prefix="/career", tags=["career"])


def _id():
    return str(uuid.uuid4())


# ---------- 职位 ----------

class PositionIn(BaseModel):
    title: str
    level: int = 1
    department: str | None = None
    required_skill_node_ids: list[str] | None = None
    next_position_id: str | None = None


@router.post("/positions", status_code=201)
async def create_position(
    data: PositionIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = CareerPosition(id=_id(), company_id=company_id,
                       **{k: v for k, v in data.model_dump().items()})
    db.add(p)
    await db.commit()
    return {"id": p.id, "title": p.title, "level": p.level}


@router.get("/positions")
async def list_positions(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(CareerPosition).where(
        (CareerPosition.company_id == company_id) |
        (CareerPosition.company_id.is_(None))).order_by(CareerPosition.level))
    return [{"id": p.id, "title": p.title, "level": p.level,
             "department": p.department} for p in q.scalars().all()]


async def _current_record(db, agent_id: str) -> AgentCareerRecord | None:
    q = await db.execute(select(AgentCareerRecord).where(
        AgentCareerRecord.agent_id == agent_id,
        AgentCareerRecord.is_current.is_(True)))
    return q.scalars().first()


# ---------- 履历 ----------

@router.post("/agents/{agent_id}/join", status_code=201)
async def join_position(
    agent_id: str,
    position_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    pos = await db.get(CareerPosition, position_id)
    if not pos:
        raise HTTPException(404, "position not found")
    cur = await _current_record(db, agent_id)
    if cur:
        raise HTTPException(409, f"已有现任职位：{cur.position_title}")
    rec = AgentCareerRecord(id=_id(), agent_id=agent_id, position_id=pos.id,
                            position_title=pos.title,
                            department=pos.department, change_type="JOINED",
                            approved_by=user.id)
    db.add(rec)
    await add_growth_event(db, agent_id, a.company_id,
                           GrowthEventType.ROLE_CHANGED,
                           f"入职岗位：{pos.title}", actor=user.id)
    await db.commit()
    return {"record_id": rec.id, "position": pos.title}


@router.get("/agents/{agent_id}/timeline")
async def career_timeline(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(AgentCareerRecord).where(
        AgentCareerRecord.agent_id == agent_id).order_by(
        AgentCareerRecord.start_date))
    return [{"title": r.position_title, "department": r.department,
             "change_type": r.change_type, "is_current": r.is_current,
             "start_date": r.start_date.isoformat(),
             "end_date": r.end_date.isoformat() if r.end_date else None}
            for r in q.scalars().all()]


# ---------- Gap 分析 ----------

@router.get("/agents/{agent_id}/gap-analysis/{target_position_id}")
async def gap_analysis(
    agent_id: str,
    target_position_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """能力缺口分析：目标职位要求的技能 vs 已掌握技能。"""
    pos = await db.get(CareerPosition, target_position_id)
    if not pos:
        raise HTTPException(404, "position not found")

    have_q = await db.execute(select(AgentSkillNode).where(
        AgentSkillNode.agent_id == agent_id))
    have_ids = {s.skill_node_id for s in have_q.scalars().all()}

    required = pos.required_skill_node_ids or []
    missing, satisfied = [], []
    for rid in required:
        node = await db.get(__import__("app.models.skill_growth",
                                       fromlist=["SkillNode"]).SkillNode, rid)
        name = node.name if node else rid
        (satisfied if rid in have_ids else missing).append(name)

    training_plan = ([f"学习技能：{m}" for m in missing]
                     + (["完成试岗任务"] if not missing else []))
    return {
        "target_position": pos.title,
        "required_skills": [n for n in (missing + satisfied)],
        "missing_skills": missing,
        "satisfied_skills": satisfied,
        "ready_for_trial": len(missing) == 0,
        "training_plan": training_plan,
    }


# ---------- 试岗 ----------

@router.post("/trials", status_code=201)
async def create_trial(
    data: dict,
    agent_id: str = Query(...),
    target_position_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t = TrialAssignment(id=_id(), agent_id=agent_id,
                        target_position_id=target_position_id,
                        task_description=data.get("task_description"))
    db.add(t)
    await db.commit()
    return {"id": t.id, "status": t.status}


class TrialReview(BaseModel):
    passed: bool
    notes: str | None = None


@router.post("/trials/{tid}/review")
async def review_trial(
    tid: str,
    data: TrialReview,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    t = await db.get(TrialAssignment, tid)
    if not t or t.status != "IN_PROGRESS":
        raise HTTPException(409, "invalid trial state")
    from datetime import datetime, timezone
    t.status = "PASSED" if data.passed else "FAILED"
    t.review_notes = data.notes
    t.reviewed_by = user.id
    t.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"id": tid, "status": t.status}


# ---------- 晋升/转岗提案 ----------

class ProposalIn(BaseModel):
    proposal_type: str            # PROMOTION / TRANSFER / DUAL_ROLE / TEMPORARY
    to_position_id: str
    reason: str | None = None


@router.post("/agents/{agent_id}/proposals", status_code=201)
async def create_proposal(
    agent_id: str,
    data: ProposalIn,
    trial_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    if data.proposal_type not in ("PROMOTION", "TRANSFER", "DUAL_ROLE", "TEMPORARY"):
        raise HTTPException(400, "invalid proposal_type")
    cur = await _current_record(db, agent_id)

    # Gap 分析快照存入提案
    gap = {}
    try:
        gap_resp = await gap_analysis(agent_id, data.to_position_id, db, user)
        gap = {"missing_skills": gap_resp["missing_skills"],
               "training_plan": gap_resp["training_plan"]}
    except HTTPException:
        pass

    prop = CareerProposal(id=_id(), agent_id=agent_id,
                          proposal_type=data.proposal_type,
                          from_position_id=cur.position_id if cur else None,
                          to_position_id=data.to_position_id,
                          gap_analysis=gap, trial_id=trial_id,
                          reason=data.reason)
    db.add(prop)
    await db.commit()
    return {"id": prop.id, "status": prop.status, "gap_analysis": gap}


@router.post("/proposals/{pid}/submit")
async def submit_proposal(
    pid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    prop = await db.get(CareerProposal, pid)
    if not prop or prop.status != "DRAFT":
        raise HTTPException(409, "只有 DRAFT 可提交")
    prop.status = "PROPOSED"
    await db.commit()
    return {"id": pid, "status": "PROPOSED"}


class CareerDecideIn(BaseModel):
    decision: str   # approve / reject
    note: str | None = None


@router.post("/proposals/{pid}/decide")
async def decide_proposal(
    pid: str,
    data: CareerDecideIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from datetime import datetime, timezone
    prop = await db.get(CareerProposal, pid)
    if not prop or prop.status != "PROPOSED":
        raise HTTPException(409, "只有 PROPOSED 可审批")
    if data.decision == "reject":
        prop.status = "REJECTED"
        prop.decided_by = user.id
        prop.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        return {"id": pid, "status": "REJECTED"}
    if data.decision != "approve":
        raise HTTPException(400, "decision must be approve/reject")

    # 批准 → 应用：结束当前任职，写入新履历
    a = await db.get(Agent, prop.agent_id)
    pos = await db.get(CareerPosition, prop.to_position_id)
    cur = await _current_record(db, prop.agent_id)
    if cur:
        cur.is_current = False
        cur.end_date = datetime.now(timezone.utc).replace(tzinfo=None)
    rec = AgentCareerRecord(
        id=_id(), agent_id=prop.agent_id, position_id=pos.id,
        position_title=pos.title, department=pos.department,
        change_type=prop.proposal_type, reason=data.note or prop.reason,
        approved_by=user.id)
    db.add(rec)

    prop.status = "APPLIED"
    prop.decided_by = user.id
    prop.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)

    ev = GrowthEventType.PROMOTED if prop.proposal_type == "PROMOTION" \
        else GrowthEventType.TRANSFERRED
    await add_growth_event(db, prop.agent_id, a.company_id, ev,
                           f"{prop.proposal_type}：→ {pos.title}",
                           source_type="career", source_id=prop.id,
                           actor=user.id)
    await db.commit()
    return {"id": pid, "status": "APPLIED", "new_position": pos.title}
