"""V5.7 ⑥⑦ 下班学习 + 高置信沉淀 API。

- POST /worklogs           员工（或代理）写日志，PENDING 等老板批
- GET  /worklogs           日志列表（可按状态/Agent 过滤）
- POST /worklogs/{id}/decide  老板 approve/reject；approve → 自动沉淀
  为高置信 AgentMemory（importance 0.95，source=worklog，多信号召回）
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.core.scope import ensure_agent_access
from app.models.agent import Agent
from app.models.memory import AgentMemory, MemoryType
from app.models.user import User
from app.models.worklog import AgentWorklog, WorklogStatus

router = APIRouter(prefix="/worklogs", tags=["worklogs"])


class WorklogIn(BaseModel):
    agent_id: str
    title: str = ""
    content: str
    lessons: list[str] | None = None
    task_refs: list[str] | None = None
    period: str = "daily"


class DecideIn(BaseModel):
    decision: str            # approve / reject
    comment: str | None = None


async def _owned_agent(db: AsyncSession, user: User, agent_id: str,
                       capability: Capability = Capability.VIEW) -> Agent:
    """Agent 归属 + 行级可见/可写范围（与 agents 列表同一套条件）。"""
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    await assert_company_capability(db, user.id, a.company_id, capability)
    await ensure_agent_access(db, user, a, capability)
    return a


@router.post("")
async def create_worklog(
    data: WorklogIn,
    agent_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """员工写工作日志（PENDING）——成长也要老板点头。"""
    a = await _owned_agent(db, user, data.agent_id, Capability.OPERATE)
    wl = AgentWorklog(
        id=str(uuid.uuid4()), agent_id=data.agent_id, company_id=a.company_id,
        period=data.period, title=data.title, content=data.content,
        lessons=data.lessons, task_refs=data.task_refs)
    db.add(wl)
    await db.commit()
    return {"id": wl.id, "status": wl.status.value}


@router.get("")
async def list_worklogs(
    agent_id: str = Query(...),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    a = await _owned_agent(db, user, agent_id)
    q = select(AgentWorklog).where(AgentWorklog.agent_id == agent_id)
    if status:
        q = q.where(AgentWorklog.status == status)
    rows = (await db.execute(
        q.order_by(AgentWorklog.created_at.desc()).limit(200))).scalars().all()
    return [{
        "id": w.id, "agent_id": w.agent_id, "period": w.period,
        "title": w.title, "content": w.content, "lessons": w.lessons,
        "task_refs": w.task_refs, "status": w.status.value,
        "memory_ref": w.memory_ref, "decision_note": w.decision_note,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    } for w in rows]


@router.post("/{wl_id}/decide")
async def decide_worklog(
    wl_id: str,
    data: DecideIn,
    agent_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """老板决策。approve → ⑦ 高置信沉淀：写 AgentMemory(importance 0.95)。"""
    # 审批并沉淀记忆 = 写操作（OPERATE），且只能操作自己写范围内的 Agent
    await _owned_agent(db, user, agent_id, Capability.OPERATE)
    wl = await db.get(AgentWorklog, wl_id)
    if not wl or wl.agent_id != agent_id:
        raise HTTPException(404, "Worklog not found")
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve/reject")
    if wl.status == WorklogStatus.APPROVED:
        return {"id": wl.id, "status": "APPROVED",
                "note": "已沉淀，幂等返回"}

    from datetime import datetime, timezone
    wl.decided_by = user.id
    wl.decision_note = data.comment
    wl.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)

    memory_ref: str | None = None
    if data.decision == "approve":
        wl.status = WorklogStatus.APPROVED
        # ⑦ 高置信沉淀（mem0 风格）：日志 + 经验点 → 记忆，
        # importance 0.95（高于普通 0.5），召回时权重最高
        mem = AgentMemory(
            id=str(uuid.uuid4()), agent_id=wl.agent_id,
            company_id=wl.company_id, memory_type=MemoryType.PROCEDURAL,
            content=f"[下班学习沉淀·{wl.period}] {wl.title or '工作日志'}\n"
                    f"{wl.content}\n"
                    + ("\n经验点：\n" + "\n".join(f"- {x}" for x in (wl.lessons or []))
                       if wl.lessons else ""),
            importance=0.95,
            keywords=[wl.period, *(wl.lessons or [])][:16],
            context_ref=wl.id,  # varchar(36)：存日志 id 作来源引用
        )
        db.add(mem)
        memory_ref = mem.id
        wl.memory_ref = mem.id
    else:
        wl.status = WorklogStatus.REJECTED
    await db.commit()
    return {"id": wl.id, "status": wl.status.value,
            "memory_ref": memory_ref,
            "note": "已沉淀为高置信记忆（importance 0.95）" if memory_ref else "已否决"}
