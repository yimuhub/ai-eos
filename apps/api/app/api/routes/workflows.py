"""Workflow CRUD + execution API (P2-02/P2-03 backend)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.core.scope import build_ctx
from app.db.session import get_db
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution,
                                 WorkflowFeedback, WorkflowNode,
                                 WorkflowStep, WorkflowStatus)
from app.schemas.responses import (WorkflowFeedbackItemOut,
                                   WorkflowFeedbackSubmitOut)
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


class WorkflowFeedbackIn(BaseModel):
    success: bool | None = None
    score: float | None = None
    lessons: list[str] = []
    note: str | None = None
    optimize: bool = True


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
             "status": w.status.value, "version": w.version,
             "source": w.source, "score": w.score, "run_count": w.run_count,
             "success_count": w.success_count,
             "failure_count": w.failure_count,
             "reuse_key": w.reuse_key or "",
             "previous_workflow_id": w.previous_workflow_id or ""}
            for w in q.scalars()]


@router.get("/workflows/{wid}")
async def get_workflow(wid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid)
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wid))
    edges_q = await db.execute(select(WorkflowEdge).where(
        WorkflowEdge.workflow_id == wid))
    feedback_q = await db.execute(
        select(WorkflowFeedback).where(WorkflowFeedback.workflow_id == wid)
        .order_by(WorkflowFeedback.created_at.desc()).limit(10))
    return {
        "id": wf.id, "name": wf.name, "description": wf.description,
        "status": wf.status.value, "version": wf.version,
        "source": wf.source, "score": wf.score, "run_count": wf.run_count,
        "success_count": wf.success_count, "failure_count": wf.failure_count,
        "reuse_key": wf.reuse_key or "",
        "previous_workflow_id": wf.previous_workflow_id or "",
        "owner_agent_id": wf.owner_agent_id or "",
        "meta": wf.meta or {},
        "nodes": [{c.key: getattr(n, c.key) for c in n.__table__.columns}
                  for n in nodes_q.scalars()],
        "edges": [{"from_node": e.from_node, "to_node": e.to_node, "label": e.label}
                  for e in edges_q.scalars()],
        "feedback": [_feedback_out(f) for f in feedback_q.scalars()],
    }


def _feedback_out(row: WorkflowFeedback) -> dict:
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "success": bool(row.success),
        "score": float(row.score or 0),
        "metrics": row.metrics or {},
        "lessons": row.lessons or [],
        "note": row.note or "",
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


@router.get("/workflows/{wid}/feedback",
            response_model=list[WorkflowFeedbackItemOut])
async def list_workflow_feedback(wid: str,
                                 limit: int = Query(20, ge=1, le=100),
                                 db: AsyncSession = Depends(get_db),
                                 user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid)
    rows = (await db.execute(
        select(WorkflowFeedback).where(WorkflowFeedback.workflow_id == wf.id)
        .order_by(WorkflowFeedback.created_at.desc()).limit(limit)
    )).scalars().all()
    return [_feedback_out(f) for f in rows]


@router.post("/workflows/{wid}/feedback",
             response_model=WorkflowFeedbackSubmitOut)
async def submit_workflow_feedback(wid: str, data: WorkflowFeedbackIn,
                                   db: AsyncSession = Depends(get_db),
                                   user=Depends(get_current_user)):
    """人工反馈一次运行结果：写入评分与经验，必要时立即升一版。

    自动反馈由 CEO 收尾时写入；这个接口给「手动跑工作流」和老板复盘用。
    """
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
    if data.score is None and data.success is None:
        raise HTTPException(400, "success 与 score 至少给一个")
    score = data.score if data.score is not None else (
        100.0 if data.success else 0.0)
    score = max(0.0, min(float(score), 100.0))
    success = data.success if data.success is not None else score >= 60

    from app.workflow_authoring import (OPTIMIZE_THRESHOLD, collect_lessons,
                                        optimize_workflow, record_feedback,
                                        workflow_brief)
    row = await record_feedback(
        db, wf, success=success, score=score, lessons=data.lessons,
        note=data.note, source="manual", created_by=user.id)
    target = wf
    optimized_from = ""
    if data.optimize and score < OPTIMIZE_THRESHOLD:
        lessons = await collect_lessons(db, wf)
        new = await optimize_workflow(db, wf, lessons)
        if new is not None:
            target = new
            optimized_from = wf.id
    await db.commit()
    await db.refresh(row)
    await db.refresh(target)
    return {"feedback": _feedback_out(row), "workflow": workflow_brief(target),
            "optimized_from": optimized_from}


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
    # 校验连线（修复前零连线/断连图也能保存，运行时执行实例会永久卡 RUNNING）：
    # 1) 非 END 节点必须有出边；2) START 必须可达 END。
    node_keys = {n.node_key for n in data.nodes}
    key_types = {n.node_key: n.node_type for n in data.nodes}
    edge_pairs = [(e.from_node, e.to_node) for e in data.edges
                  if e.from_node in node_keys and e.to_node in node_keys
                  and e.from_node != e.to_node]
    if not edge_pairs:
        raise HTTPException(400, "工作流没有任何连线：请从节点右侧圆点拖到另一节点连线后再保存")
    out_map: dict[str, list[str]] = {}
    for s, d2 in edge_pairs:
        out_map.setdefault(s, []).append(d2)
    no_out = sorted(k for k, t in key_types.items()
                    if t != NodeType.END.value and not out_map.get(k))
    if no_out:
        raise HTTPException(400, f"这些节点没有连到下一步：{', '.join(no_out[:5])}（每个非结束节点都要有出边）")
    seen_reach: set[str] = set()
    stack = [k for k, t in key_types.items() if t == NodeType.START.value]
    while stack:
        k = stack.pop()
        if k in seen_reach:
            continue
        seen_reach.add(k)
        stack.extend(out_map.get(k, []))
    ends = {k for k, t in key_types.items() if t == NodeType.END.value}
    if not (seen_reach & ends):
        raise HTTPException(400, "开始节点连不到结束节点，请检查连线方向")

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


@router.put("/workflows/{wid}")
async def update_workflow(wid: str, data: WorkflowSaveIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """更新已有工作流（修复前只有 POST：编辑保存会新建重复定义）。"""
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
    # 与新建同一套图校验（见 save_workflow：非 END 必须有出边、START 可达 END）
    types = {n.node_type for n in data.nodes}
    if NodeType.START not in types or NodeType.END not in types:
        raise HTTPException(400, "工作流必须包含开始和结束节点")
    node_keys = {n.node_key for n in data.nodes}
    key_types = {n.node_key: n.node_type for n in data.nodes}
    edge_pairs = [(e.from_node, e.to_node) for e in data.edges
                  if e.from_node in node_keys and e.to_node in node_keys
                  and e.from_node != e.to_node]
    if not edge_pairs:
        raise HTTPException(400, "工作流没有任何连线：请从节点右侧圆点拖到另一节点连线后再保存")
    out_map: dict[str, list[str]] = {}
    for s, d2 in edge_pairs:
        out_map.setdefault(s, []).append(d2)
    no_out = sorted(k for k, t in key_types.items()
                    if t != NodeType.END.value and not out_map.get(k))
    if no_out:
        raise HTTPException(400, f"这些节点没有连到下一步：{', '.join(no_out[:5])}")
    seen_reach: set[str] = set()
    stack = [k for k, t in key_types.items() if t == NodeType.START.value]
    while stack:
        k = stack.pop()
        if k in seen_reach:
            continue
        seen_reach.add(k)
        stack.extend(out_map.get(k, []))
    ends = {k for k, t in key_types.items() if t == NodeType.END.value}
    if not (seen_reach & ends):
        raise HTTPException(400, "开始节点连不到结束节点，请检查连线方向")

    from sqlalchemy import delete as _sa_delete
    wf.name = data.name
    wf.description = data.description
    await db.execute(_sa_delete(WorkflowNode).where(WorkflowNode.workflow_id == wid))
    await db.execute(_sa_delete(WorkflowEdge).where(WorkflowEdge.workflow_id == wid))
    for n in data.nodes:
        nt = NodeType(n.node_type)
        db.add(WorkflowNode(workflow_id=wid, node_key=n.node_key,
                            node_type=nt, name=n.name,
                            config=n.config or {},
                            position_x=n.position_x, position_y=n.position_y))
    for e in data.edges:
        db.add(WorkflowEdge(workflow_id=wid, from_node=e.from_node,
                            to_node=e.to_node, label=e.label))
    wf.version = (wf.version or 1) + 1
    await log_event(db, "workflow.updated", "workflow_definition", wid, "user",
                    user.id, {"name": data.name, "version": wf.version})
    await db.commit()
    return {"id": wid, "name": wf.name, "version": wf.version}


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
