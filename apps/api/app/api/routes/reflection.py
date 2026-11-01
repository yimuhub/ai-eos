"""Reflection API (P3-03)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_agent_owned, get_current_user, get_db
from app.core.permissions import (Capability, ensure_capability,
                                  require_company_scope)
from app.models.agent import Agent
from app.models.company import Department
from app.models.reflection import (AgentReflection, ExperienceLesson,
                                   ImprovementProposal, ReflectionTrigger)
from app.models.user import User
from app.schemas.responses import (AgentsOverviewOut, LessonDeletedOut,
                                   LessonOut, ProposalDecisionOut,
                                   ReflectionOut)
from app.reflection_engine import record_experience, reflect

router = APIRouter(prefix="/reflection", tags=["reflection"],
                   dependencies=[Depends(require_company_scope())])
class ExperienceIn(BaseModel):
    agent_id: str
    title: str
    outcome: str                      # SUCCESS / FAILED / PARTIAL
    task_id: str | None = None
    project_id: str | None = None
    summary: str | None = None
    user_feedback: str | None = None
    execution_output: str | None = None
    errors: str | None = None


@router.post("/experiences", status_code=201)
async def create_experience(
    data: ExperienceIn,
    trigger: ReflectionTrigger = Query(ReflectionTrigger.MANUAL),
    do_reflect: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录工作经历并（可选）立即触发 LLM 复盘。"""
    a = await assert_agent_owned(db, user.id, data.agent_id)  # 归属校验
    if data.outcome not in ("SUCCESS", "FAILED", "PARTIAL"):
        raise HTTPException(400, "outcome must be SUCCESS/FAILED/PARTIAL")

    exp = await record_experience(db, a.id, a.company_id,
                                  data.title,
                                  f"outcome={data.outcome}; {data.summary or ''}"[:300])
    result: dict = {"experience_id": exp.id}
    if do_reflect:
        try:
            refl_result = await reflect(db, exp, trigger)
            summary_text = ""
            if isinstance(refl_result, dict):
                summary_text = refl_result.get("summary", "")
            elif hasattr(refl_result, "summary"):
                summary_text = refl_result.summary
            if summary_text:
                from app.models.reflection import ExperienceLesson
                lesson_row = ExperienceLesson(id=str(__import__("uuid").uuid4()),
                    agent_id=exp.agent_id, reflection_id=None,
                    lesson=summary_text[:500], tags=["reflection"])
                db.add(lesson_row)
                await db.commit()
            result.update({"reflection_summary": summary_text[:300],
                           "reflect_success": bool(summary_text)})
        except Exception as e:
            result["reflect_error"] = str(e)[:200]
    return result


@router.get("/agents/{agent_id}/lessons", response_model=list[LessonOut])
async def lessons(
    agent_id: str,
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """该 Agent 的经验教训库（可用于后续任务的 prompt 注入）。"""
    await assert_agent_owned(db, user.id, agent_id)  # 归属校验
    q = await db.execute(select(ExperienceLesson).where(
        ExperienceLesson.agent_id == agent_id).order_by(
        ExperienceLesson.created_at.desc()).limit(limit))
    return [{"id": l.id, "lesson": l.lesson, "tags": l.tags,
             "created_at": l.created_at.isoformat()} for l in q.scalars().all()]


@router.delete("/agents/{agent_id}/lessons/{lesson_id}",
               response_model=LessonDeletedOut)
async def delete_lesson(
    agent_id: str,
    lesson_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除一条经验教训。

    ## 改造说明
    前端「复盘中心」和「员工档案」两个页面都实现了"删除这条复盘"，
    调用的是 `DELETE /reflection/agents/{id}/lessons/{lesson_id}`，
    但后端**从未实现该路由** → 用户点删除永远 404，
    且前端用 `catch {}` 静默吞掉，表现为"点了没反应"。
    这里补上实现：既修复断链，也补上记录级归属校验。
    """
    await assert_agent_owned(db, user.id, agent_id)

    row = await db.get(ExperienceLesson, lesson_id)
    # 同时校验记录本身归属：防止用自己 Agent 的 id 删除他人记录
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "经验教训不存在")

    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted_id": lesson_id}


@router.get("/agents/{agent_id}/reflections",
            response_model=list[ReflectionOut])
async def reflections(
    agent_id: str,
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_agent_owned(db, user.id, agent_id)  # 归属校验
    q = await db.execute(select(AgentReflection).where(
        AgentReflection.agent_id == agent_id).order_by(
        AgentReflection.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    out = []
    for r in rows:
        pq = await db.execute(select(ImprovementProposal).where(
            ImprovementProposal.reflection_id == r.id))
        out.append({
            "id": r.id, "trigger": r.trigger.value,
            "went_well": r.went_well, "what_failed": r.what_failed,
            "root_cause": r.root_cause,
            "created_at": r.created_at.isoformat(),
            "proposals": [{"id": p.id, "title": p.title, "status": p.status,
                           "type": p.proposal_type} for p in pq.scalars().all()]})
    return out


class DecideIn(BaseModel):
    decision: str   # approve / reject
    apply: bool = False   # approve 时是否直接应用（写入档案/知识库）


@router.post("/proposals/{pid}/decide",
             response_model=ProposalDecisionOut)
async def decide_proposal(
    pid: str,
    data: DecideIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """治理审批改进提案（不批准绝不生效）。"""
    from datetime import datetime, timezone
    prop = await db.get(ImprovementProposal, pid)
    if not prop:
        raise HTTPException(404, "not found")
    # router 级 require_company_scope 在无 company_id 时跳过，而本端点只带 pid，
    # 因此必须在这里按提案 → Agent → 公司反查归属。Agent 缺失也不放行：
    # 无法确定租户归属时就当作提案不存在。
    agent = await db.get(Agent, prop.agent_id)
    if not agent:
        raise HTTPException(404, "not found")
    # 审批会写知识库/学习档案（apply=true 时），按 MANAGE 收口；跨租户 404。
    await ensure_capability(db, user.id, agent.company_id, Capability.MANAGE)
    if prop.status != "PROPOSED":
        raise HTTPException(409, f"status={prop.status}")
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve/reject")

    prop.status = "APPROVED" if data.decision == "approve" else "REJECTED"
    prop.decided_by = user.id
    prop.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)

    applied = False
    if data.decision == "approve" and data.apply:
        # 应用：knowledge_update 写知识库；其余记学习目标
        payload = prop.payload or {}
        content = payload.get("content") or prop.description or ""
        if prop.proposal_type == "knowledge_update" and content.strip():
            from app.knowledge_engine import ingest_document
            from app.models.knowledge import KBDocument
            doc = KBDocument(id=__import__("uuid").uuid4().__str__(),
                             company_id=agent.company_id,
                             title=f"[改进]{prop.title}",
                             source_type="text", content=content)
            db.add(doc)
            await db.flush()
            await ingest_document(db, doc)
            applied = True
        else:
            from app.models.learning import AgentLearningRecord
            db.add(AgentLearningRecord(id=__import__("uuid").uuid4().__str__(),
                                       agent_id=prop.agent_id,
                                       goal=prop.description or prop.title))
            applied = True

    await db.commit()
    return {"id": pid, "status": prop.status, "applied": applied}


@router.get("/agents-overview", response_model=AgentsOverviewOut)
async def agents_overview(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按部门聚合所有员工的教训数。"""
    agents = (await db.execute(select(Agent).where(Agent.company_id == company_id))).scalars().all()
    depts = (await db.execute(select(Department).where(Department.company_id == company_id))).scalars().all()
    # 按 agent 统计教训数（从 agent_lessons 或 reflection 表）
    try:
        from app.models.reflection import ExperienceLesson
        lr = (await db.execute(
            select(ExperienceLesson.agent_id, sa_func.count()).group_by(ExperienceLesson.agent_id)
        )).all()
        lesson_count = {a_: c for a_, c in lr}
    except Exception:
        lesson_count = {}
    # 按部门聚合
    from collections import defaultdict
    dept_agents: dict[str, list] = defaultdict(list)
    for a in agents:
        dept_agents[str(a.department_id or "")].append({
            "id": a.id, "name": a.name,
            "role": a.role or None, "status": a.status,
            "lesson_count": lesson_count.get(a.id, 0),
        })
    # 按部门 id 保留顺序
    out = []
    for d in depts:
        ags = dept_agents.get(str(d.id), [])
        if ags:
            out.append({"dept_id": d.id, "dept_name": d.name, "agents": ags})
    # 无部门员工
    if "" in dept_agents:
        out.append({"dept_id": "", "dept_name": "无部门", "agents": dept_agents[""]})
    return {"departments": out}
