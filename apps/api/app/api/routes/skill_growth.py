"""Skill Growth API (P3-04): 技能树 + 等级 + 证据。"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.learning_engine import add_growth_event
from app.models.agent import Agent
from app.models.learning import GrowthEventType
from app.models.skill_growth import (LEVEL_ORDER, AgentSkillNode, SkillEvaluation,
                                     SkillLevel, SkillNode, new_id)
from app.models.user import User

router = APIRouter(prefix="/skill-growth", tags=["skill-growth"])


# ---------- 技能树 ----------

class NodeIn(BaseModel):
    key: str
    name: str
    description: str | None = None
    parent_id: str | None = None
    prerequisite_id: str | None = None


@router.post("/nodes", status_code=201)
async def create_node(
    data: NodeIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node = SkillNode(id=new_id(), **data.model_dump())
    db.add(node)
    await db.commit()
    return {"id": node.id, "key": node.key}


@router.get("/tree")
async def get_tree(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回全部技能节点（前端组装树形）。"""
    q = await db.execute(select(SkillNode).where(
        (SkillNode.company_id == company_id) | (SkillNode.company_id.is_(None))))
    nodes = q.scalars().all()
    return [{"id": n.id, "key": n.key, "name": n.name, "parent_id": n.parent_id,
             "prerequisite_id": n.prerequisite_id} for n in nodes]


# ---------- Agent 技能掌握 ----------

class AcquireIn(BaseModel):
    skill_node_id: str
    source_type: str = "assignment"   # assignment/training/evaluation/experience
    evidence: list[dict] | None = None


@router.post("/agents/{agent_id}/acquire", status_code=201)
async def acquire_skill(
    agent_id: str,
    data: AcquireIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 获得新技能（BEGINNER 起步，附证据）。"""
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    node = await db.get(SkillNode, data.skill_node_id)
    if not node:
        raise HTTPException(404, "Skill node not found")

    # 前置技能检查
    if node.prerequisite_id:
        pre = await db.execute(select(AgentSkillNode).where(
            AgentSkillNode.agent_id == agent_id,
            AgentSkillNode.skill_node_id == node.prerequisite_id))
        if not pre.scalars().first():
            raise HTTPException(409, "前置技能未掌握")

    existing = await db.execute(select(AgentSkillNode).where(
        AgentSkillNode.agent_id == agent_id,
        AgentSkillNode.skill_node_id == node.id))
    if existing.scalars().first():
        raise HTTPException(409, "已拥有该技能")

    asn = AgentSkillNode(
        id=new_id(), agent_id=agent_id, skill_node_id=node.id,
        level=SkillLevel.BEGINNER, evidence=data.evidence or [],
        source_type=data.source_type)
    db.add(asn)
    await add_growth_event(db, agent_id, a.company_id,
                           GrowthEventType.SKILL_ADDED,
                           f"获得技能：{node.name}",
                           detail={"level": "BEGINNER",
                                   "evidence_count": len(data.evidence or [])},
                           source_type=data.source_type, actor=user.id)
    await db.commit()
    return {"id": asn.id, "level": "BEGINNER"}


class EvidenceIn(BaseModel):
    evidence_type: str      # task / training / evaluation
    ref_id: str | None = None
    note: str | None = None


@router.post("/agent-skills/{asn_id}/evidence")
async def add_evidence(
    asn_id: str,
    data: EvidenceIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """添加使用证据（experience_count+1，等级升级的依据）。"""
    asn = await db.get(AgentSkillNode, asn_id)
    if not asn:
        raise HTTPException(404, "not found")
    ev_list = list(asn.evidence or [])
    ev_list.append({"type": data.evidence_type, "ref": data.ref_id,
                    "note": data.note,
                    "at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc).isoformat()})
    asn.evidence = ev_list
    asn.experience_count += 1
    asn.last_used_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"experience_count": asn.experience_count, "level": asn.level.value}


class UpgradeIn(BaseModel):
    new_level: SkillLevel
    score: float | None = None
    method: str = "user_confirm"
    evidence_summary: str | None = None


@router.post("/agent-skills/{asn_id}/upgrade")
async def upgrade_skill(
    asn_id: str,
    data: UpgradeIn,
    require_evidence: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """升级等级：必须有证据支撑（禁止虚假声称）。"""
    asn = await db.get(AgentSkillNode, asn_id)
    if not asn:
        raise HTTPException(404, "not found")

    cur = LEVEL_ORDER[asn.level.value]
    tgt = LEVEL_ORDER[data.new_level.value]
    if tgt != cur + 1:
        raise HTTPException(409, f"只能逐级升级（当前 {asn.level.value}）")

    min_evidence = (cur + 1) * 2  # 升级所需最低证据数：2/4/6/8
    ev_count = len(asn.evidence or [])
    if require_evidence and ev_count < min_evidence:
        raise HTTPException(409,
                            f"证据不足：需要 {min_evidence} 条，当前 {ev_count} 条"
                            "（禁止无证据升级）")

    node = await db.get(SkillNode, asn.skill_node_id)
    prev = asn.level
    asn.level = data.new_level
    asn.evaluation_score = data.score
    asn.last_evaluated_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).replace(tzinfo=None)
    db.add(SkillEvaluation(
        id=new_id(), agent_id=asn.agent_id, skill_node_id=asn.skill_node_id,
        previous_level=prev, new_level=data.new_level, score=data.score,
        method=data.method, evidence_summary=data.evidence_summary,
        evaluated_by=user.id))
    a = await db.get(Agent, asn.agent_id)
    await add_growth_event(db, asn.agent_id, a.company_id,
                           GrowthEventType.SKILL_UPGRADED,
                           f"技能升级：{node.name} → {data.new_level.value}",
                           detail={"score": data.score,
                                   "evidence_count": ev_count},
                           actor=user.id)
    await db.commit()
    return {"level": asn.level.value, "evidence_used": ev_count}


@router.post("/agent-skills/{asn_id}/rollback/{to_level}")
async def rollback_skill(
    asn_id: str,
    to_level: SkillLevel,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """回滚等级。"""
    asn = await db.get(AgentSkillNode, asn_id)
    if not asn:
        raise HTTPException(404, "not found")
    old = asn.level
    asn.level = to_level
    a = await db.get(Agent, asn.agent_id)
    node = await db.get(SkillNode, asn.skill_node_id)
    await add_growth_event(db, asn.agent_id, a.company_id,
                           GrowthEventType.SKILL_UPGRADED,
                           f"技能回滚：{node.name} {old.value}→{to_level.value}",
                           actor=user.id)
    await db.commit()
    return {"level": asn.level.value}


@router.get("/agents/{agent_id}/matrix")
async def skill_matrix(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent Skill Matrix（合并成长中心 + 技能中心两套数据源）。"""
    from app.models.skill import AgentSkill as AgentSkillRow, Skill as SkillRow
    out = []
    seen_names: set[str] = set()

    # 数据源A：成长中心体系（agent_skill_nodes，带等级/证据）
    q = await db.execute(select(AgentSkillNode).where(
        AgentSkillNode.agent_id == agent_id))
    for r in q.scalars().all():
        node = await db.get(SkillNode, r.skill_node_id)
        name = node.name if node else "?"
        seen_names.add(name)
        out.append({"id": r.id, "skill": name,
                    "level": r.level.value,
                    "experience_count": r.experience_count,
                    "evidence_count": len(r.evidence or []),
                    "evaluation_score": r.evaluation_score,
                    "source_type": r.source_type})

    # 数据源B：技能中心体系（agent_skills，已安装技能）
    q2 = await db.execute(
        select(AgentSkillRow.id.label("as_id"), SkillRow.name)
        .join(SkillRow, SkillRow.id == AgentSkillRow.skill_id)
        .where(AgentSkillRow.agent_id == agent_id))
    for row in q2.mappings().all():
        if row["name"] in seen_names:
            continue
        out.append({"id": row["as_id"], "skill": row["name"],
                    "level": "BEGINNER",
                    "experience_count": 0, "evidence_count": 0,
                    "evaluation_score": None, "source_type": "installed"})
    return out


class BatchAcquireIn(BaseModel):
    """批量学习：skill_node_ids 列表，一次性添加多个技能。"""
    skill_node_ids: list[str]
    source_type: str = "assignment"
    evidence: list[dict] | None = None


@router.post("/agents/{agent_id}/acquire-batch", status_code=201)
async def acquire_skill_batch(
    agent_id: str,
    data: BatchAcquireIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 批量获得新技能（BEGINNER 起步）。"""
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")

    added = []
    skipped = []
    for nid in data.skill_node_ids:
        node = await db.get(SkillNode, nid)
        if not node:
            skipped.append(nid)
            continue

        # 前置技能检查
        if node.prerequisite_id:
            pre = await db.execute(select(AgentSkillNode).where(
                AgentSkillNode.agent_id == agent_id,
                AgentSkillNode.skill_node_id == node.prerequisite_id))
            if not pre.scalars().first():
                skipped.append(nid)
                continue

        existing = await db.execute(select(AgentSkillNode).where(
            AgentSkillNode.agent_id == agent_id,
            AgentSkillNode.skill_node_id == node.id))
        if existing.scalars().first():
            skipped.append(nid)
            continue

        asn = AgentSkillNode(
            id=new_id(), agent_id=agent_id, skill_node_id=node.id,
            level=SkillLevel.BEGINNER, evidence=data.evidence or [],
            source_type=data.source_type)
        db.add(asn)
        added.append(node.name)

    if added:
        await add_growth_event(db, agent_id, a.company_id,
                               GrowthEventType.SKILL_ADDED,
                               f"批量获得技能：{', '.join(added)}",
                               detail={"count": len(added), "skipped": len(skipped)},
                               source_type=data.source_type, actor=user.id)
        await db.commit()

    return {"added": added, "skipped": skipped}


@router.delete("/agents/{agent_id}/skills/{asn_id}", status_code=200)
async def remove_agent_skill(agent_id: str, asn_id: str,
                              company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """移除 Agent 的一项技能（从综合档案中删减）。"""
    # 原先只比较"该 Agent 的公司 == 调用者传入的公司"，两边都可被调用者控制，
    # 等于没有鉴权：传别人的 company_id 就能削减别人员工的技能。
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, company_id)
    a = await db.get(Agent, agent_id)
    if not a or a.company_id != company_id:
        raise HTTPException(404)
    asn = await db.get(AgentSkillNode, asn_id)
    if asn and asn.agent_id == agent_id:
        await db.delete(asn)
        await db.commit()
        return {"ok": True}
    # 技能中心体系：尝试删 agent_skills 记录
    from app.models.skill import AgentSkill as AgentSkillRow
    ask = await db.get(AgentSkillRow, asn_id)
    if ask and ask.agent_id == agent_id:
        await db.delete(ask)
        await db.commit()
        return {"ok": True}
    raise HTTPException(404)
