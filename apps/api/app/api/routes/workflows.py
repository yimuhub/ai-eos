"""Workflow CRUD + execution API (P2-02/P2-03 backend)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.company import Company
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep, WorkflowStatus)
from app.services.event_system import log_event

router = APIRouter(tags=["workflows"])


class NodeIn(BaseModel):
    node_key: str
    node_type: str
    name: str
    config: dict | None = {}
    position_x: float = 0
    position_y: float = 0


class EdgeIn(BaseModel):
    from_node: str
    to_node: str
    label: str | None = None


class WorkflowSaveIn(BaseModel):
    name: str
    description: str | None = None
    project_id: str | None = None
    nodes: list[NodeIn]
    edges: list[EdgeIn]


async def _wf_owned(db: AsyncSession, user_id: str, wf_id: str) -> WorkflowDefinition:
    wf = await db.get(WorkflowDefinition, wf_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    c = await db.get(Company, wf.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.get("/workflows")
async def list_workflows(company_id: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    q = await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.company_id == company_id))
    return [{"id": w.id, "name": w.name, "description": w.description,
             "status": w.status.value} for w in q.scalars()]


@router.get("/workflows/{wid}")
async def get_workflow(wid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user.id, wid)
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wid))
    edges_q = await db.execute(select(WorkflowEdge).where(
        WorkflowEdge.workflow_id == wid))
    return {
        "id": wf.id, "name": wf.name, "description": wf.description,
        "status": wf.status.value,
        "nodes": [{c.key: getattr(n, c.key) for c in n.__table__.columns}
                  for n in nodes_q.scalars()],
        "edges": [{"from_node": e.from_node, "to_node": e.to_node, "label": e.label}
                  for e in edges_q.scalars()],
    }


async def start_execution(db: AsyncSession, workflow_id: str,
                          project_id: str | None, started_by: str,
                          input_data: dict | None = None) -> WorkflowExecution:
    from app.workflow_runtime import advance_execution, start_workflow
    ex = await start_workflow(db, workflow_id, started_by, project_id, input_data)
    return ex


@router.post("/workflows", status_code=201)
async def save_workflow(data: WorkflowSaveIn, company_id: str,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")

    # 校验：必须有 START 和 END
    types = {n.node_type for n in data.nodes}
    if NodeType.START not in types or NodeType.END not in types:
        raise HTTPException(400, "工作流必须包含开始和结束节点")

    wf = WorkflowDefinition(company_id=company_id, project_id=data.project_id,
                            name=data.name, description=data.description,
                            status=WorkflowStatus.PUBLISHED, created_by=user.id)
    db.add(wf)
    await db.flush()
    for n in data.nodes:
        try:
            nt = NodeType(n.node_type)
        except ValueError:
            raise HTTPException(400, f"未知节点类型 {n.node_type}")
        db.add(WorkflowNode(workflow_id=wf.id, node_key=n.node_key,
                            node_type=nt, name=n.name,
                            config=n.config or {},
                            position_x=n.position_x, position_y=n.position_y))
    for e in data.edges:
        db.add(WorkflowEdge(workflow_id=wf.id, from_node=e.from_node,
                            to_node=e.to_node, label=e.label))
    await log_event(db, "workflow.saved", "workflow_definition", None, "user",
                    user.id, {"name": data.name, "nodes": len(data.nodes)})
    await db.commit()
    return {"id": wf.id, "name": wf.name}


@router.delete("/workflows/{wid}", status_code=204)
async def delete_workflow(wid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    wf = await _wf_owned(db, user.id, wid)
    await db.delete(wf)
    await db.commit()


# ---------- Execution ----------
def _run_in_background(execution_id: str):
    import app.background_jobs as background_jobs

    async def _job(session):
        from app.core.config import settings
        settings.debug = False
        from app.workflow_runtime import advance_execution
        await advance_execution(session, execution_id)

    background_jobs.submit_async(_job)


@router.post("/workflows/{wid}/run", status_code=202)
async def run_workflow(wid: str, background: BackgroundTasks,
                       project_id: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user.id, wid)
    if wf.status == WorkflowStatus.ARCHIVED:
        raise HTTPException(400, "工作流已归档，不能执行")
    ex = await start_execution(db, wid, project_id, user.id)
    execution_id = ex.id
    _run_in_background(execution_id)
    return {"detail": "started", "execution_id": execution_id}


@router.get("/executions/{eid}")
async def get_execution(eid: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await _wf_owned(db, user.id, ex.workflow_id)

    steps_q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == eid).order_by(WorkflowStep.created_at))
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == ex.workflow_id))
    node_names = {n.node_key: n.name for n in nodes_q.scalars()}

    steps = []
    for s in steps_q.scalars():
        d = {c.key: getattr(s, c.key) for c in s.__table__.columns}
        d["node_type"] = s.node_type.value
        d["status"] = s.status.value
        d["node_name"] = node_names.get(s.node_key, s.node_key)
        steps.append(d)

    return {"id": ex.id, "workflow_id": ex.workflow_id, "workflow_name": wf.name,
            "project_id": ex.project_id, "status": ex.status.value,
            "current_nodes": ex.current_nodes, "error": ex.error,
            "steps": steps}


@router.post("/executions/{eid}/pause")
async def pause_execution(eid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    ex = await _owned_execution(db, user.id, eid)
    if ex.status not in (RunStatus.RUNNING, RunStatus.WAITING_INPUT,
                         RunStatus.WAITING_APPROVAL):
        raise HTTPException(409, f"cannot pause from {ex.status.value}")
    ex.status = RunStatus.PAUSED
    await db.commit()
    return {"detail": "paused"}


@router.post("/executions/{eid}/resume")
async def resume_execution(eid: str, background: BackgroundTasks,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user.id, eid)
    if ex.status != RunStatus.PAUSED:
        raise HTTPException(409, f"cannot resume from {ex.status.value}")
    ex.status = RunStatus.RUNNING
    await db.commit()
    _run_in_background(eid)
    return {"detail": "resumed"}


@router.post("/executions/{eid}/cancel")
async def cancel_execution(eid: str, reason: str | None = None,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user.id, eid)
    if ex.status in (RunStatus.COMPLETED, RunStatus.CANCELLED):
        raise HTTPException(409, f"cannot cancel from {ex.status.value}")
    ex.status = RunStatus.CANCELLED
    ex.error = f"cancelled by user: {reason or 'no reason'}"
    await db.commit()
    return {"detail": "cancelled"}


@router.post("/executions/{eid}/approve/{node_key}")
async def approve_node(eid: str, node_key: str, approve: bool = True,
                       human_input: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """人工审批/输入后继续执行。"""
    ex = await _owned_execution(db, user.id, eid)
    if ex.status not in (RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT):
        raise HTTPException(409, f"execution is {ex.status.value}, not waiting")

    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == ex.workflow_id,
        WorkflowNode.node_key == node_key))
    node = nodes_q.scalars().first()
    if not node:
        raise HTTPException(404, "node not found")

    cfg = dict(node.config or {})
    if node.node_type == NodeType.APPROVAL:
        cfg["_approved"] = approve
    elif node.node_type == NodeType.HUMAN_INPUT:
        cfg["_human_input"] = human_input or ""
    node.config = cfg
    ex.status = RunStatus.RUNNING
    await db.commit()

    if approve or node.node_type == NodeType.HUMAN_INPUT:
        _run_in_background(eid)
        return {"detail": "approved, continuing"}
    # 拒绝审批 → 取消执行
    ex.status = RunStatus.CANCELLED
    ex.error = "rejected at approval node"
    await db.commit()
    return {"detail": "rejected, execution cancelled"}


async def _owned_execution(db: AsyncSession, user_id: str, eid: str) -> WorkflowExecution:
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    await _wf_owned(db, user_id, ex.workflow_id)
    return ex
