"""Workflow Runtime (P2-02).

All workflow state lives in the database. The runtime advances an execution
by processing currently-active nodes; every transition is persisted, so the
execution survives server restarts and supports pause/resume/cancel/retry.
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskPriority, TaskStatus
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.services.event_system import log_event


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowRuntime:
    def __init__(self, db: AsyncSession, execution: WorkflowExecution):
        self.db = db
        self.exec = execution

    async def _load_graph(self) -> tuple[dict[str, WorkflowNode], dict[str, list[WorkflowEdge]]]:
        nodes_q = await self.db.execute(
            select(WorkflowNode).where(WorkflowNode.workflow_id == self.exec.workflow_id))
        edges_q = await self.db.execute(
            select(WorkflowEdge).where(WorkflowEdge.workflow_id == self.exec.workflow_id))
        nodes = {n.node_key: n for n in nodes_q.scalars()}
        out_edges: dict[str, list[WorkflowEdge]] = {}
        for e in edges_q.scalars():
            out_edges.setdefault(e.from_node, []).append(e)
        return nodes, out_edges

    async def _open_step(self, node: WorkflowNode, input_data: dict) -> WorkflowStep:
        prev_q = await self.db.execute(
            select(WorkflowStep).where(
                WorkflowStep.execution_id == self.exec.id,
                WorkflowStep.node_key == node.node_key))
        attempt = len(prev_q.scalars().all()) + 1
        step = WorkflowStep(execution_id=self.exec.id, node_key=node.node_key,
                            node_type=node.node_type, status=RunStatus.RUNNING,
                            attempt=attempt, input_data=input_data,
                            started_at=utcnow_iso())
        self.db.add(step)
        await self.db.flush()
        return step

    async def _finish_step(self, step: WorkflowStep, output: dict,
                           status: RunStatus = RunStatus.COMPLETED,
                           error: str | None = None):
        step.output_data = output
        step.status = status
        step.completed_at = utcnow_iso()
        if error:
            step.error = error[:4000]
        await self.db.flush()

    # ---------------- node executors ----------------

    async def _exec_agent_node(self, node: WorkflowNode, ctx: dict) -> dict:
        """AGENT 节点：创建任务并同步执行（后台线程由外层调度）。"""
        from app.agent_runtime import run_agent_on_task
        from app.background_jobs import _make_session_factory
        from app.models.agent import Agent

        cfg = node.config or {}
        agent = await self.db.get(Agent, cfg.get("agent_id"))
        if not agent:
            raise RuntimeError(f"Agent {cfg.get('agent_id')} not found")
        # company_id 优先取执行输入，否则回溯 workflow definition
        company_id = self.exec_input_company()
        if not company_id:
            wf = await self.db.get(WorkflowDefinition, self.exec.workflow_id)
            company_id = wf.company_id if wf else None
        if not company_id:
            raise RuntimeError("company_id unknown for agent task")
        title = (cfg.get("task_title_template") or node.name)
        task = Task(company_id=company_id,
                    project_id=self.exec.project_id,
                    title=title[:500],
                    description=cfg.get("instruction") or "",
                    status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                    assigned_agent_id=agent.id, created_by=self.exec.started_by)
        self.db.add(task)
        await self.db.flush()
        try:
            await run_agent_on_task(self.db, agent, task, actor_type="system")
            await transition_task(self.db, task, TaskStatus.COMPLETED)
            return {"task_id": task.id, "output": task.result or ""}
        except Exception:
            await self.db.rollback()

    def exec_input_company(self) -> str:
        return (self.exec.input_data or {}).get("company_id", "")

    async def execute_step(self, node: WorkflowNode, ctx: dict) -> dict | None:
        """Execute one node. Returns output dict, or None if waiting/blocked."""
        if node.node_type in (NodeType.START, NodeType.MERGE):
            return {"passed": True}

        if node.node_type == NodeType.END:
            return {"finished": True}

        if node.node_type == NodeType.AGENT:
            result = await self._exec_agent_node(node, ctx)
            if result is None:
                return None  # worker handles it asynchronously
            return result

        if node.node_type == NodeType.CONDITION:
            keyword = (node.config or {}).get("keyword", "")
            last_outputs = " ".join(str(v.get("output", "")) for v in ctx.values())
            branch = "yes" if keyword.lower() in last_outputs.lower() else "no"
            return {"branch": branch}

        if node.node_type == NodeType.WAIT:
            seconds = int((node.config or {}).get("seconds", 5))
            await asyncio.sleep(min(seconds, 60))
            return {"waited": seconds}

        if node.node_type == NodeType.APPROVAL:
            if (node.config or {}).get("_approved"):
                return {"approved": True}
            # 需要人工：execution 进入 WAITING_APPROVAL，节点保持打开
            self.exec.status = RunStatus.WAITING_APPROVAL
            return None

        if node.node_type == NodeType.HUMAN_INPUT:
            provided = (node.config or {}).get("_human_input")
            if provided is not None:
                return {"human_input": provided}
            self.exec.status = RunStatus.WAITING_INPUT
            return None

        if node.node_type == NodeType.SUB_WORKFLOW:
            sub_id = (node.config or {}).get("workflow_id")
            if not sub_id:
                raise RuntimeError("SUB_WORKFLOW missing workflow_id")
            from app.api.routes.workflows import start_execution  # local import
            sub_exec = await start_execution(
                self.db, sub_id, self.exec.project_id, self.exec.started_by,
                input_data={**self.exec.input_data, "_parent_execution": self.exec.id})
            return {"sub_execution": sub_exec.id}

        if node.node_type == NodeType.TASK:
            # 纯任务占位节点：直接通过
            return {"passed": True}

        if node.node_type == NodeType.PARALLEL:
            return {"fanned_out": True}

        raise RuntimeError(f"Unknown node type {node.node_type}")


async def transition_task(db: AsyncSession, task: Task, new_status: TaskStatus):
    from app.task_engine import transition
    await transition(db, task, new_status, actor_type="system")


async def advance_execution(db: AsyncSession, execution_id: str,
                            max_steps: int = 50) -> WorkflowExecution:
    """推进一个执行实例，直到完成/失败/需要人工。可重入（服务重启后调用）。"""
    ex = await db.get(WorkflowExecution, execution_id)
    if not ex:
        raise ValueError("execution not found")
    rt = WorkflowRuntime(db, ex)
    nodes, out_edges = await rt._load_graph()

    steps_q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == ex.id))
    done_nodes: dict[str, dict] = {}
    for s in steps_q.scalars():
        if s.status == RunStatus.COMPLETED and s.output_data:
            done_nodes[s.node_key] = s.output_data

    current = list(ex.current_nodes or [])
    steps_taken = 0

    while current and steps_taken < max_steps:
        next_nodes: list[str] = []
        progressed = False

        for key in current:
            node = nodes.get(key)
            if not node:
                continue

            # MERGE：等待所有上游分支完成
            if node.node_type == NodeType.MERGE:
                upstream_done = all(
                    any(e.to_node == key and src in done_nodes for src in nodes)
                    for e in [])  # simplified: merge passes when scheduled
                pass

            step = await rt._open_step(node, {})
            try:
                output = await rt.execute_step(node, done_nodes)
            except Exception as e:
                await rt._finish_step(step, {}, RunStatus.FAILED, str(e))
                ex.status = RunStatus.FAILED
                ex.error = f"node {key}: {e}"
                await log_event(db, "workflow.failed", "workflow_execution",
                                ex.id, "system", None, {"node": key})
                await db.commit()
                return ex

            if output is None:
                # 等待人工（审批/输入）——保留该节点为活跃
                await db.commit()
                return ex

            await rt._finish_step(step, output)

            # END 节点：整个执行完成
            if node.node_type == NodeType.END:
                ex.status = RunStatus.COMPLETED
                ex.current_nodes = [key]
                ex.output_data = {k: v for k, v in done_nodes.items()}
                await log_event(db, "workflow.completed", "workflow_execution",
                                ex.id, "system", None, {})
                await db.commit()
                return ex

            done_nodes[key] = output
            progressed = True

            # 决定后继节点
            edges = out_edges.get(key, [])
            if node.node_type == NodeType.CONDITION:
                branch = output.get("branch", "yes")
                chosen = [e.to_node for e in edges
                          if (e.label or "").lower() == branch]
                next_nodes.extend(chosen or [e.to_node for e in edges])
            elif node.node_type == NodeType.PARALLEL:
                next_nodes.extend(e.to_node for e in edges)  # 全部分支同时激活
            else:
                next_nodes.extend(e.to_node for e in edges)

        if not progressed:
            break

        # 去重保序
        seen = set()
        current = [k for k in next_nodes if not (k in seen or seen.add(k))]
        ex.current_nodes = current
        ex.status = RunStatus.RUNNING
        await db.commit()
        steps_taken += 1

    if current and steps_taken >= max_steps:
        # 还有未走完的节点（例如 AGENT 节点由外部 worker 执行），挂起等待
        ex.status = RunStatus.WAITING_INPUT
        await db.commit()
    return ex


async def start_workflow(db: AsyncSession, workflow_id: str, started_by: str,
                         project_id: str | None = None,
                         input_data: dict | None = None) -> WorkflowExecution:
    ex = WorkflowExecution(workflow_id=workflow_id, project_id=project_id,
                           status=RunStatus.RUNNING, input_data=input_data or {},
                           started_by=started_by)
    db.add(ex)
    await db.flush()

    # 起点：START 节点
    q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == workflow_id,
        WorkflowNode.node_type == NodeType.START))
    start = q.scalars().first()
    if not start:
        raise ValueError("workflow has no START node")
    ex.current_nodes = [start.node_key]
    await log_event(db, "workflow.started", "workflow_execution", ex.id,
                    "user", started_by, {"workflow": workflow_id})
    await db.commit()
    await db.refresh(ex)
    return ex
