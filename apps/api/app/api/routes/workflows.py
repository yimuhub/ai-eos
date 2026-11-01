"""Workflow CRUD + execution API (P2-02/P2-03 backend)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.core.scope import build_ctx
from app.db.session import get_db
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


def _workflow_scope(ctx):
    """工作流行的可见条件：owner/admin 看全公司，其余只看自己创建的。

    WorkflowDefinition 没有部门字段，只能按创建者收窄——
    宁可让成员少看到一些，也不把别人的编排与执行历史摊开。
    """
    base = WorkflowDefinition.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    return and_(base, WorkflowDefinition.created_by == ctx.user_id)


async def _wf_owned(db: AsyncSession, user, wf_id: str,
                    capability: Capability = Capability.VIEW) -> WorkflowDefinition:
    wf = await db.get(WorkflowDefinition, wf_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await assert_company_capability(db, user.id, wf.company_id, capability)
    ctx = await build_ctx(db, user, wf.company_id)
    found = (await db.execute(
        select(WorkflowDefinition.id).where(WorkflowDefinition.id == wf.id,
                                            _workflow_scope(ctx))
        .limit(1))).first()
    if found is None:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.get("/workflows")
async def list_workflows(company_id: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    ctx = await build_ctx(db, user, company_id)
    q = await db.execute(select(WorkflowDefinition).where(_workflow_scope(ctx)))
    return [{"id": w.id, "name": w.name, "description": w.description,
             "status": w.status.value} for w in q.scalars()]


@router.get("/workflows/{wid}")
async def get_workflow(wid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid)
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
    # 保存/覆盖编排是日常写操作（OPERATE）
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)

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
    # 删工作流不可逆（节点/连线一并消失），按 DESTRUCTIVE 处理
    wf = await _wf_owned(db, user, wid, Capability.DESTRUCTIVE)
    await db.delete(wf)
    await db.commit()


# ---------- Execution ----------
def _run_in_background(execution_id: str):
    import app.background_jobs as background_jobs

    async def _job(session):
        from app.workflow_runtime import advance_execution
        await advance_execution(session, execution_id)

    background_jobs.submit_async(_job)


@router.post("/workflows/{wid}/run", status_code=202)
async def run_workflow(wid: str, background: BackgroundTasks,
                       project_id: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
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
    wf = await _wf_owned(db, user, ex.workflow_id)

    # 重启/丢失调度的惰性恢复：状态还在跑但没有任何 worker 持有租约时，
    # 看板轮询到这里就把实例重新踢起来（租约保证不会和在跑的 worker 双跑）。
    from app.workflow_runtime import execution_is_orphaned
    if await execution_is_orphaned(db, ex):
        await log_event(db, "workflow.recovered", "workflow_execution", eid,
                        "system", None, {"status": ex.status.value})
        await db.commit()
        _run_in_background(eid)

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
    ex = await _owned_execution(db, user, eid)
    # CAS：只有仍在"可暂停"状态的那一次生效。推进循环每步前重读状态，
    # 所以暂停能立刻打断后续节点。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.in_([
                   RunStatus.RUNNING, RunStatus.PENDING,
                   RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL]))
        .values(status=RunStatus.PAUSED)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot pause from {ex.status.value}")
    return {"detail": "paused"}


@router.post("/executions/{eid}/resume")
async def resume_execution(eid: str, background: BackgroundTasks,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user, eid)
    # CAS PAUSED→PENDING：并发点两次「继续」只会有一次提交后台推进，
    # 也避免第二次把正在 RUNNING 的实例再拉一份 worker。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status == RunStatus.PAUSED)
        .values(status=RunStatus.PENDING)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot resume from {ex.status.value}")
    _run_in_background(eid)
    return {"detail": "resumed"}


@router.post("/executions/{eid}/cancel")
async def cancel_execution(eid: str, reason: str | None = None,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user, eid)
    # CAS：取消对"还没结束"的实例都生效（含等人工/暂停/排队），
    # 推进循环每步前重读状态，读到 CANCELLED 立刻停手。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.notin_([RunStatus.COMPLETED,
                                                RunStatus.CANCELLED]))
        .values(status=RunStatus.CANCELLED,
                error=f"cancelled by user: {reason or 'no reason'}")
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot cancel from {ex.status.value}")
    await log_event(db, "workflow.cancelled", "workflow_execution", eid,
                    "user", user.id, {"reason": reason})
    await db.commit()
    return {"detail": "cancelled"}


@router.post("/executions/{eid}/approve/{node_key}")
async def approve_node(eid: str, node_key: str, approve: bool = True,
                       human_input: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """人工审批/输入后继续执行。"""
    ex = await _owned_execution(db, user, eid)

    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == ex.workflow_id,
        WorkflowNode.node_key == node_key))
    node = nodes_q.scalars().first()
    if not node:
        raise HTTPException(404, "node not found")

    # 先抢占"继续权"（CAS 等人工 → PENDING），再写节点配置并提交后台推进：
    # 并发审批/双击「通过」只会有一个请求真正续跑。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.in_([RunStatus.WAITING_APPROVAL,
                                             RunStatus.WAITING_INPUT]))
        .values(status=RunStatus.CANCELLED
                if not approve and node.node_type == NodeType.APPROVAL
                else RunStatus.PENDING,
                error=("rejected at approval node"
                       if not approve and node.node_type == NodeType.APPROVAL
                       else None))
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        await db.commit()
        await db.refresh(ex)
        raise HTTPException(409, f"execution is {ex.status.value}, not waiting")

    cfg = dict(node.config or {})
    if node.node_type == NodeType.APPROVAL:
        cfg["_approved"] = approve
    elif node.node_type == NodeType.HUMAN_INPUT:
        cfg["_human_input"] = human_input or ""
    node.config = cfg
    await db.commit()

    if approve or node.node_type == NodeType.HUMAN_INPUT:
        _run_in_background(eid)
        return {"detail": "approved, continuing"}
    # 拒绝审批 → 取消执行（状态已在上面 CAS 里落成 CANCELLED）
    return {"detail": "rejected, execution cancelled"}


async def _owned_execution(db: AsyncSession, user, eid: str) -> WorkflowExecution:
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    # 跑/停/继续/审批都是对编排的写操作
    await _wf_owned(db, user, ex.workflow_id, Capability.OPERATE)
    return ex
