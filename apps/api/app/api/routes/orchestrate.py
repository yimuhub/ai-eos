"""Multi-agent orchestrated task execution endpoint (P1-10)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import AsyncSessionLocal, get_db
from app.models.agent import Agent, AgentStatus
from app.models.company import Company
from app.models.task import Task, TaskPriority, TaskStatus
from app.orchestrator import orchestrate
from app.services.event_system import log_event
from app.task_engine import transition

router = APIRouter(tags=["orchestration"])


class OrchestrateIn(BaseModel):
    company_id: str
    title: str
    description: str | None = None
    coordinator_agent_id: str


@router.post("/orchestrate", status_code=202)
async def orchestrate_task(data: OrchestrateIn,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    coordinator = await db.get(Agent, data.coordinator_agent_id)
    if not coordinator:
        raise HTTPException(404, "Coordinator agent not found")

    parent = Task(company_id=data.company_id, title=data.title,
                  description=data.description, status=TaskStatus.PLANNING,
                  priority=TaskPriority.NORMAL, assigned_agent_id=data.coordinator_agent_id,
                  created_by=user.id)
    db.add(parent)
    await log_event(db, "orchestration.started", "task", None, "user", user.id,
                    {"title": data.title})
    await db.commit()
    await db.refresh(parent)
    task_id = parent.id

    async def _run(session):
        p = await session.get(Task, task_id)
        coord = await session.get(Agent, data.coordinator_agent_id)
        from app.models.task import TaskStatus as TS
        try:
            
            if p.status == TS.PLANNING:
                await transition(session, p, TS.RUNNING, "system", None)
            await orchestrate(session, p, coord)
            await transition(session, p, TS.COMPLETED, "agent", coord.id)
        except Exception:
            import traceback
            traceback.print_exc()
            await session.rollback()
            p2 = await session.get(Task, task_id)
            if p2 and p2.status in (TS.PLANNING, TS.RUNNING):
                p2.status = TS.FAILED
            c2 = await session.get(Agent, data.coordinator_agent_id)
            if c2 and c2.status.value == "RUNNING":
                c2.status = AgentStatus.IDLE
            await log_event(session, "orchestration.failed", "task", task_id,
                            "system", None, {"error": "see server log"})
            await session.commit()

    import app.background_jobs as background_jobs
    background_jobs.submit_async(_run)

    return {"detail": "orchestration started", "task_id": task_id}
