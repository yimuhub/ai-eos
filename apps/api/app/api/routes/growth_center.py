"""成长中心 (P4)：按部门聚合成长档案 + 学习计划管理。"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import JSON, String, Text, Float, select, Column
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.core.scope import agent_read_scope, build_ctx
from app.db.base import Base
from app.models.agent import Agent
from app.models.user import User
from app.models.memory import AgentMemory

router = APIRouter(prefix="/growth-center", tags=["growth-center"])


from sqlalchemy import Column as _Col, DateTime as _DT, func as _func


class TS:
    created_at = _Col("created_at", _DT(timezone=True), server_default=_func.now())
    updated_at = _Col("updated_at", _DT(timezone=True), server_default=_func.now(),
                      onupdate=_func.now())


class LearningPlan(Base, TS):
    """学习计划：老板为 Agent 设定的周期性学习任务。"""
    __tablename__ = "learning_plans"
    id = Column(String(36), primary_key=True)
    company_id = Column(String(36), index=True)
    agent_id = Column(String(36), index=True)
    title = Column(String(200))
    target_skills = Column(JSON, default=list)     # 目标技能名列表
    knowledge_scope = Column(Text)                 # 知识/知识库范围说明
    period_days = Column(Float, default=7)         # 学习周期（天）
    start_at = Column(_DT(timezone=True))
    due_at = Column(_DT(timezone=True))
    status = Column(String(20), default="active")  # active|completed|cancelled
    progress = Column(Float, default=0)            # 0~100
    notes = Column(Text)


class PlanIn(BaseModel):
    title: str
    agent_ids: list[str]
    target_skills: list[str] = []
    knowledge_scope: str | None = None
    period_days: float = 7
    notes: str | None = None


async def _owned_company(db, user_id, company_id,
                 capability: Capability = Capability.VIEW):
    """公司门禁：角色具备 capability 才放行，其余一律 404。

    P5 之后成员/管理员身份由 resolve_role 解析，不再只认 companies.owner_id。
    """
    return await assert_company_capability(db, user_id, company_id, capability)


@router.get("/departments-overview")
async def departments_overview(company_id: str = Query(...),
                               db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """按一级部门聚合全部员工的成长档案摘要。"""
    await _owned_company(db, user.id, company_id)
    ctx = await build_ctx(db, user, company_id)
    agents = (await db.execute(select(Agent).where(
        agent_read_scope(ctx)))).scalars().all()
    from app.models.company import Department
    depts = (await db.execute(select(Department).where(
        Department.company_id == company_id))).scalars().all()
    dept_map = {d.id: d.name for d in depts}

    # 技能掌握数 + 记忆数（轻量聚合）
    # 必须按公司过滤：agent_skills 本身没有 company_id，用本公司 Agent 子查询
    # 收窄；agent_memories 直接带 company_id。此前两者都是全表聚合——一家公司
    # 的部门概览会把别家数据也数进去，且随平台总数据量线性变慢。
    from app.models.skill import AgentSkill
    company_agent_ids = select(Agent.id).where(Agent.company_id == company_id)
    ask_rows = (await db.execute(select(AgentSkill.agent_id).where(
        AgentSkill.agent_id.in_(company_agent_ids)))).scalars().all()
    skill_count: dict[str, int] = {}
    for aid_ in ask_rows:
        skill_count[aid_] = skill_count.get(aid_, 0) + 1

    from sqlalchemy import func as F
    mr = (await db.execute(select(
        AgentMemory.agent_id, F.count()).where(
        AgentMemory.company_id == company_id).group_by(AgentMemory.agent_id))).all()
    mem_count = {a_: c for a_, c in mr}

    plans = (await db.execute(select(LearningPlan).where(
        LearningPlan.company_id == company_id,
        LearningPlan.status == "active"))).scalars().all()
    plan_by_agent: dict[str, int] = {}
    for p_ in plans:
        plan_by_agent[p_.agent_id] = plan_by_agent.get(p_.agent_id, 0) + 1

    groups: dict[str, dict] = {}
    for a in agents:
        key = a.department_id or "__none__"
        g = groups.setdefault(key, {
            "dept_id": key,
            "dept_name": dept_map.get(a.department_id) if a.department_id else "未分配部门",
            "agents": []})
        g["agents"].append({
            "id": str(a.id), "name": a.name, "role": a.role,
            "status": a.status,
            "skill_count": skill_count.get(str(a.id), 0),
            "memory_count": mem_count.get(str(a.id), 0),
            "active_plans": plan_by_agent.get(str(a.id), 0),
        })
    return {"departments": list(groups.values())}


@router.get("/plans")
async def list_plans(company_id: str = Query(...),
                     agent_id: str | None = Query(None),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id)
    ctx = await build_ctx(db, user, company_id)
    visible_agents = select(Agent.id).where(agent_read_scope(ctx))
    q = select(LearningPlan).where(
        LearningPlan.company_id == company_id,
        LearningPlan.agent_id.in_(visible_agents))
    if agent_id:
        q = q.where(LearningPlan.agent_id == agent_id)
    rows = (await db.execute(q.order_by(LearningPlan.created_at.desc()))).scalars().all()
    name_map = {}
    agents = (await db.execute(select(Agent).where(
        agent_read_scope(ctx)))).scalars().all()
    name_map = {str(a.id): a.name for a in agents}
    return [{"id": p.id, "title": p.title, "agent_id": p.agent_id,
             "agent_name": name_map.get(p.agent_id, "?"),
             "target_skills": p.target_skills, "knowledge_scope": p.knowledge_scope,
             "period_days": p.period_days, "status": p.status,
             "progress": p.progress, "notes": p.notes,
             "created_at": str(p.created_at)} for p in rows]


@router.post("/plans", status_code=201)
async def create_plan(data: PlanIn, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """创建学习计划（可批量分配给多名员工）。"""
    import uuid as _uuid
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    now = datetime.now(timezone.utc)
    created = []
    for aid in data.agent_ids:
        p = LearningPlan(
            id=_uuid.uuid4().hex[:32], company_id=company_id, agent_id=aid,
            title=data.title, target_skills=data.target_skills,
            knowledge_scope=data.knowledge_scope,
            period_days=data.period_days, start_at=now,
            due_at=now.replace(microsecond=0), status="active",
            progress=0, notes=data.notes)
        # due_at 按 period_days 推
        from datetime import timedelta
        p.due_at = now + timedelta(days=data.period_days)
        db.add(p)
        created.append(p.id)
    await db.commit()
    return {"created": len(created)}


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: str, company_id: str = Query(...),
                      status: str | None = Query(None),
                      progress: float | None = Query(None),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    p = await db.get(LearningPlan, plan_id)
    if not p or p.company_id != company_id:
        raise HTTPException(404)
    if status in ("active", "completed", "cancelled"):
        p.status = status
        if status == "completed":
            p.progress = 100
    if progress is not None:
        p.progress = max(0, min(100, progress))
    await db.commit()
    return {"ok": True}


@router.get("/agents/{agent_id}/detail")
async def agent_detail(agent_id: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """成长中心弹窗详情：技能清单 / 记忆清单 / 进行中计划（可管理）。"""
    await _owned_company(db, user.id, company_id)
    target = await db.get(Agent, agent_id)
    if not target or target.company_id != company_id:
        raise HTTPException(404, "Agent not found")
    # 行级可见性：成员只能看自己范围内的员工档案
    from app.core.scope import ensure_agent_access
    await ensure_agent_access(db, user, target, Capability.VIEW)
    from app.models.skill import AgentSkill, Skill as SkillRow
    from app.models.memory import AgentMemory
    skills = (await db.execute(
        select(SkillRow.name, SkillRow.scope, SkillRow.source,
               AgentSkill.id.label("asn_id"))
        .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
        .where(AgentSkill.agent_id == agent_id)
        .distinct(SkillRow.id))).mappings().all()

    memories = (await db.execute(select(AgentMemory).where(
        AgentMemory.agent_id == agent_id).order_by(
        AgentMemory.created_at.desc()))).scalars().all()

    plans = (await db.execute(select(LearningPlan).where(
        LearningPlan.agent_id == agent_id,
        LearningPlan.company_id == company_id,
        LearningPlan.status == "active"))).scalars().all()

    return {
        "skills": [{"id": s["asn_id"], "name": s["name"],
                    "scope": s["scope"], "source": s["source"]} for s in skills],
        "memories": [{"id": m.id, "type": str(m.memory_type),
                      "content": m.content[:200],
                      "created_at": str(m.created_at)[:10]} for m in memories],
        "plans": [{"id": p.id, "title": p.title, "progress": p.progress,
                   "period_days": p.period_days,
                   "target_skills": p.target_skills} for p in plans],
    }


@router.delete("/skills/{asn_id}")
async def delete_agent_skill(asn_id: str, company_id: str = Query(...),
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """删除员工的一项技能掌握记录。

    修复两处缺陷：
    1. 原代码 `from app.models.skill import AgentSkillNode`（该模型不存在）
       并直接使用未导入的 `AgentSkill` → 必然 NameError → 500。
    2. 原代码只校验了 company_id 归属，未校验被删记录本身属于该公司，
       知道 id 即可跨租户删除。现按 agent→company 显式校验。
    """
    await _owned_company(db, user.id, company_id, Capability.MANAGE)
    from app.models.skill import AgentSkill

    row = await db.get(AgentSkill, asn_id)
    if not row:
        raise HTTPException(404, "技能记录不存在")
    # 跨租户保护：记录所属 Agent 必须属于当前公司
    owner = await db.get(Agent, row.agent_id)
    if not owner or owner.company_id != company_id:
        raise HTTPException(404, "技能记录不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.delete("/memories/{memory_id}")
async def delete_agent_memory(memory_id: str, company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """删除员工的一条长期记忆（错误或累赘的记忆）。

    同 delete_agent_skill：补上记录级的租户归属校验，避免跨公司删除。
    """
    await _owned_company(db, user.id, company_id, Capability.MANAGE)
    m = await db.get(AgentMemory, memory_id)
    if not m:
        raise HTTPException(404, "记忆不存在")
    owner = await db.get(Agent, m.agent_id)
    if not owner or owner.company_id != company_id:
        raise HTTPException(404, "记忆不存在")
    await db.delete(m)
    await db.commit()
    return {"ok": True}


class AddMemoryIn(BaseModel):
    agent_id: str
    company_id: str
    content: str
    memory_type: str = "EPISODIC"  # EPISODIC|SEMANTIC|PROCEDURAL|WORKING|REFLECTION


@router.post("/memories", status_code=201)
async def add_agent_memory(data: AddMemoryIn,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """为员工添加一条长期记忆。"""
    await _owned_company(db, user.id, data.company_id, Capability.OPERATE)
    a = await db.get(Agent, data.agent_id)
    if not a or a.company_id != data.company_id:
        raise HTTPException(404)
    from app.core.scope import ensure_agent_access
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    from app.models.memory import MemoryType
    m = AgentMemory(
        id=str(uuid.uuid4()), agent_id=data.agent_id, company_id=data.company_id,
        memory_type=MemoryType[data.memory_type.upper()], content=data.content)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id, "ok": True}


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """删除学习计划。"""
    await _owned_company(db, user.id, company_id, Capability.MANAGE)
    p = await db.get(LearningPlan, plan_id)
    if not p or p.company_id != company_id:
        raise HTTPException(404)
    await db.delete(p)
    await db.commit()
    return {"ok": True}
