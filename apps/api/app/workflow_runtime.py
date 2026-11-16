"""Workflow Runtime (P2-02).

All workflow state lives in the database. The runtime advances an execution
by processing currently-active nodes; every transition is persisted, so the
execution survives server restarts and supports pause/resume/cancel/retry.

并发与取消（P0-6）：
- 同一个执行实例同一时刻只允许一个 worker 推进（runtime_locks 租约
  `workflow:<id>`，10 分钟自动过期）——重复调度、重启恢复、用户连点「运行」
  都只会有一个赢家，不会同一批节点跑两遍（每个 AGENT 节点都会建任务+计费）。
- 取消/暂停立即生效：推进循环每步前重读状态，读到 CANCELLED/PAUSED 就退出，
  并且所有状态落地都用 CAS（只有仍处于预期状态才写），不会把已取消的实例
  又写回 RUNNING/COMPLETED。
- 长耗时节点（AGENT/WAIT）之前先 commit，不持写锁跨网络调用。
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.logging_config import get_logger
from app.models.task import Task, TaskPriority, TaskStatus
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution, WorkflowNode,
                                 WorkflowStep)
from app.services.event_system import log_event

logger = get_logger(__name__)

# 租约时长：要覆盖一个长 AGENT 节点（LLM 调用）+ WAIT(≤60s)，又不能让
# 崩溃后的实例永久卡住。
WORKFLOW_LEASE_S = 10 * 60


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
            # 此前 `except Exception: await self.db.rollback()` 会隐式返回 None，
            # 被上层 execute_step 当作 "worker 异步处理/等待人工" commit 掉，
            # 本步记录也被回滚抹掉 → 实例永久挂起。
            # 现在：把任务标 FAILED（证据落库由 run_agent_on_task 失败路径负责），
            # 再原样上抛，交给 advance_execution 对称地写 _finish_step(FAILED)+commit。
            try:
                await transition_task(self.db, task, TaskStatus.FAILED)
            except Exception:
                logger.warning("任务状态转换失败，直接置 FAILED 并通知：task=%s",
                               task.id, exc_info=True)
                task.status = TaskStatus.FAILED
                from app.services.notification_service import notify_task_terminal
                await notify_task_terminal(self.db, task, "FAILED",
                                           error="工作流节点执行失败")
            raise

    def exec_input_company(self) -> str:
        return (self.exec.input_data or {}).get("company_id", "")

    async def execute_step(self, node: WorkflowNode, ctx: dict) -> dict | None:
        """Execute one node. Returns output dict, or None if waiting/blocked."""
        if node.node_type in (NodeType.START, NodeType.MERGE):
            return {"passed": True}

        if node.node_type == NodeType.END:
            return {"finished": True}

        if node.node_type == NodeType.AGENT:
            # _exec_agent_node 要么返回 output dict，要么上抛异常（标 FAILED），
            # 不会再返回 None —— None 只保留给真正的"等待人工"（审批/输入）语义。
            return await self._exec_agent_node(node, ctx)

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

        if node.node_type == NodeType.HTTP:
            return await self._exec_http_node(node)

        if node.node_type == NodeType.TOOL:
            return await self._exec_tool_node(node)

        if node.node_type == NodeType.TRANSFORM:
            return self._exec_transform_node(node, ctx)

        raise RuntimeError(f"Unknown node type {node.node_type}")

    # ---------------- 基座三节点（V6 M1-e） ----------------

    def _resolve_company(self) -> str:
        company_id = self.exec_input_company()
        if not company_id:
            # 同步查询（调用点都持有会话）；没有就向上抛，让节点失败可诊断
            return ""
        return company_id

    async def _exec_http_node(self, node: WorkflowNode) -> dict:
        """HTTP 节点：直调外部 API。

        安全：endpoint 走工具总线同款公网闸门（回环放行、私网/元数据拒绝）；
        凭据不进 config——auth_secret_ref 指向 company_secrets，取值走保险库。
        非 2xx 不抛异常（返回结构化结果给 CONDITION 分支），网络异常才失败。
        """
        import json as _json

        from app.core.http_client import http_client
        from app.toolbus.dispatch import _assert_public_http_url

        cfg = node.config or {}
        url = str(cfg.get("url") or "").strip()
        if not url:
            raise RuntimeError("HTTP 节点缺少 url")
        _assert_public_http_url(url)
        method = str(cfg.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise RuntimeError(f"不支持的 HTTP 方法 {method}")
        timeout = min(max(1, int(cfg.get("timeout_seconds") or 15)), 120)

        headers = {str(k): str(v) for k, v in
                   (cfg.get("headers") or {}).items()}
        secret_ref = cfg.get("auth_secret_ref")
        if secret_ref:
            from sqlalchemy import select as _select
            from app.models.company_secret import CompanySecret
            from app.services.secret_vault import decrypt
            row = (await self.db.execute(
                _select(CompanySecret).where(
                    CompanySecret.company_id == self._resolve_company(),
                    CompanySecret.name == secret_ref))).scalars().first()
            if row is None:
                raise RuntimeError(f"HTTP 节点凭据 {secret_ref} 不存在")
            headers["Authorization"] = f"Bearer {decrypt(row.ciphertext)}"

        body = cfg.get("body")
        if body is not None and not isinstance(body, (str, type(None))):
            body = _json.dumps(body, ensure_ascii=False)
        async with http_client(timeout=timeout) as client:
            resp = await client.request(
                method, url, headers=headers,
                content=body if method in ("POST", "PUT", "PATCH") else None)
        text = (resp.text or "")[:8000]
        out = {"ok": 200 <= resp.status_code < 300,
               "status": resp.status_code, "body": text}
        try:
            out["json"] = _json.loads(text)
        except (_json.JSONDecodeError, ValueError):
            pass  # noqa: silent-except — 非 JSON 响应体是常态（HTML/纯文本），如实留在 body 字段
        # 出站 DLP（V6 M5）：HTTP 响应与工具结果同一道闸门
        try:
            from app.services.dlp import mask_result
            out, findings = mask_result(out)
            if findings:
                out["dlp_findings"] = findings[:10]
        except Exception:  # noqa: BLE001 — 脱敏层故障不阻塞流程
            logger.warning("HTTP 节点 DLP 脱敏失败 node=%s", node.node_key,
                           exc_info=True)
        return out

    async def _exec_tool_node(self, node: WorkflowNode) -> dict:
        """TOOL 节点：调工具总线（web/电脑/公司登记的动态工具）。

        电脑工具会经过 approval_gate——审批被 hold 时本节点产出
        awaiting_approval 结果并继续（人工批准后重跑该工作流）。
        """
        from app.toolbus import execute_bus_tool, get_tool

        cfg = node.config or {}
        tool_name = str(cfg.get("tool_name") or "").strip()
        if not tool_name:
            raise RuntimeError("TOOL 节点缺少 tool_name")
        company_id = self._resolve_company()
        if not get_tool(tool_name):
            from app.toolbus import has_db_tool
            if not company_id or not await has_db_tool(self.db, company_id,
                                                       tool_name):
                raise RuntimeError(f"工具 {tool_name} 未注册（内置或本公司动态工具）")
        result = await execute_bus_tool(
            tool_name, cfg.get("arguments") or {}, company_id=company_id,
            user_id=self.exec.started_by)
        return {"tool": tool_name, "result": result}

    def _exec_transform_node(self, node: WorkflowNode, ctx: dict) -> dict:
        """TRANSFORM 节点：把上游产出映射成下游需要的形状。

        模板语法：`"{{节点key.路径.字段}}"` 取上游 step 输出；
        `"{{input.xxx}}"` 取执行入参；非字符串 = 字面量。
        取不到的路径解析为 None（宁可显式空值也不静默串改上一节点的数据）。
        """
        cfg = node.config or {}
        mapping = cfg.get("set") or {}
        if not isinstance(mapping, dict):
            raise RuntimeError("TRANSFORM 节点 config.set 必须是对象")
        sources = {"input": dict(self.exec.input_data or {})}
        sources.update({k: v for k, v in ctx.items()})

        def lookup(root_key: str, path: list[str]):
            cur = sources.get(root_key)
            for seg in path:
                if isinstance(cur, dict):
                    cur = cur.get(seg)
                elif isinstance(cur, list) and seg.lstrip("-").isdigit():
                    try:
                        cur = cur[int(seg)]
                    except IndexError:
                        return None
                else:
                    return None
            return cur

        data: dict = {}
        for out_key, tpl in mapping.items():
            if isinstance(tpl, str) and tpl.startswith("{{") \
                    and tpl.endswith("}}"):
                parts = tpl[2:-2].strip().split(".")
                data[out_key] = lookup(parts[0], parts[1:])
            else:
                data[out_key] = tpl
        return {"data": data}


async def transition_task(db: AsyncSession, task: Task, new_status: TaskStatus):
    from app.task_engine import transition
    await transition(db, task, new_status, actor_type="system")


async def advance_execution(db: AsyncSession, execution_id: str,
                            max_steps: int = 50) -> WorkflowExecution:
    """推进一个执行实例，直到完成/失败/需要人工。可重入（服务重启后调用）。

    互斥由 `workflow:<id>` 租约保证：拿不到锁说明已有 worker 在推进
    （或刚崩溃、租约未过期），本次直接返回当前状态而不是并行再跑一遍。
    """
    from app.task_engine import acquire_lock, new_lock_owner, release_lock

    lock_key = f"workflow:{execution_id}"
    holder = new_lock_owner()
    if not await acquire_lock(db, lock_key, holder, ttl_seconds=WORKFLOW_LEASE_S):
        ex = await db.get(WorkflowExecution, execution_id)
        if ex is None:
            raise ValueError("execution not found")
        logger.info("[workflow %s] 已有 worker 在推进，跳过重复调度", execution_id)
        return ex
    try:
        return await _advance_locked(db, execution_id, max_steps=max_steps)
    finally:
        try:
            await release_lock(db, lock_key, holder)
        except Exception as lease_err:  # 释放失败最多等租约自然过期
            logger.warning("[workflow %s] 释放租约失败: %s", execution_id, lease_err)


async def _cas_status(db: AsyncSession, execution_id: str,
                      expected: RunStatus, new: RunStatus, **extra) -> bool:
    """只有当前仍处于 expected 才写 new（避免把已取消的实例"复活"）。"""
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == execution_id,
               WorkflowExecution.status == expected)
        .values(status=new, **extra)
        .execution_options(synchronize_session=False))
    return res.rowcount == 1


async def _advance_locked(db: AsyncSession, execution_id: str,
                          max_steps: int = 50) -> WorkflowExecution:
    ex = await db.get(WorkflowExecution, execution_id)
    if not ex:
        raise ValueError("execution not found")

    # 终态 / 等人工 / 已暂停：不自动推进
    if ex.status in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED,
                     RunStatus.PAUSED, RunStatus.WAITING_APPROVAL,
                     RunStatus.WAITING_INPUT):
        return ex

    # PENDING→RUNNING 用 CAS 落地：这是"本次由我推进"的唯一凭据。
    if ex.status == RunStatus.PENDING:
        res = await _cas_status(db, execution_id, RunStatus.PENDING,
                                RunStatus.RUNNING)
        await db.commit()
        if not res:
            await db.refresh(ex)
            return ex
        set_committed_value(ex, "status", RunStatus.RUNNING)

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

            # 恢复推进防重放（修复前会把已完成节点再跑一遍：重复建任务/重复计费）：
            # done_nodes 只含有 COMPLETED 输出的节点；等待人工的节点不会进入
            # done_nodes（output=None 提前 return），所以"已完成即跳过"是精确的。
            if key in done_nodes:
                progressed = True
                continue

            # 取消/暂停立即生效：每步前重读状态。读到就退出，不再开新节点。
            await db.refresh(ex)
            if ex.status in (RunStatus.CANCELLED, RunStatus.PAUSED,
                             RunStatus.FAILED):
                return ex

            # MERGE：汇聚语义按"先到先过"处理——并行分支在同一个 advance 里
            # 顺序执行完再调度 MERGE（next_nodes 去重后只进一次）；若分支跨轮
            # 完成，后到分支的出边会再次调度 MERGE，但此时 MERGE 已在
            # done_nodes 里，上面的跳过逻辑保证下游子树不会执行两遍。
            if node.node_type == NodeType.MERGE:
                pass

            step = await rt._open_step(node, {})
            # 先把 step 行（RUNNING）落库并释放写锁，再去调可能很慢的节点
            await db.commit()
            try:
                output = await rt.execute_step(node, done_nodes)
            except Exception as e:
                await rt._finish_step(step, {}, RunStatus.FAILED, str(e))
                if await _cas_status(db, ex.id, RunStatus.RUNNING,
                                     RunStatus.FAILED,
                                     error=f"node {key}: {e}"):
                    set_committed_value(ex, "status", RunStatus.FAILED)
                    ex.error = f"node {key}: {e}"
                await log_event(db, "workflow.failed", "workflow_execution",
                                ex.id, "system", None, {"node": key})
                await db.commit()
                return ex

            if output is None:
                # 等待人工（审批/输入）——保留该节点为活跃
                await db.commit()
                return ex

            # 节点执行期间用户可能已取消：结果不再写回，也不再推进后继节点
            await db.refresh(ex)
            if ex.status in (RunStatus.CANCELLED, RunStatus.PAUSED,
                             RunStatus.FAILED):
                await rt._finish_step(
                    step, output,
                    RunStatus.CANCELLED if ex.status == RunStatus.CANCELLED
                    else RunStatus.PENDING)
                await db.commit()
                return ex

            await rt._finish_step(step, output)

            # END 节点：整个执行完成
            if node.node_type == NodeType.END:
                if await _cas_status(db, ex.id, RunStatus.RUNNING,
                                     RunStatus.COMPLETED,
                                     current_nodes=[key]):
                    set_committed_value(ex, "status", RunStatus.COMPLETED)
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

        # 去重保序。注意这里**只写 current_nodes**（不写 status）：
        # 状态若被并发取消成 CANCELLED，一个属于 RUNNING→RUNNING 的赋值
        # 会在 flush 时把它覆盖回去。
        seen = set()
        current = [k for k in next_nodes if not (k in seen or seen.add(k))]
        ex.current_nodes = current
        await db.commit()
        steps_taken += 1

    if current and steps_taken >= max_steps:
        # 还有未走完的节点（例如 AGENT 节点由外部 worker 执行），挂起等待
        if await _cas_status(db, ex.id, RunStatus.RUNNING,
                             RunStatus.WAITING_INPUT):
            set_committed_value(ex, "status", RunStatus.WAITING_INPUT)
            ex.current_nodes = current
        await db.commit()
    elif ex.status == RunStatus.RUNNING:
        # 收尾兜底（修复前直接 return，执行实例带着空 current_nodes 永久
        # RUNNING，看板轮询反复空转自愈）：
        # - current 为空：走到了没有出边的非 END 节点（图不完整/断连）；
        # - current 非空但全部已完成：永远到不了 END（缺 END 连线）。
        # 两种都判 FAILED，把"图不完整"如实暴露给用户，而不是静默挂死。
        reason = ("流程在非结束节点处没有后继节点（图不完整或存在断连）"
                  if not current else
                  "剩余节点均已执行但未到达结束节点（请检查连线后重试）")
        if await _cas_status(db, ex.id, RunStatus.RUNNING, RunStatus.FAILED,
                             error=reason):
            set_committed_value(ex, "status", RunStatus.FAILED)
            ex.error = reason
        await log_event(db, "workflow.failed", "workflow_execution",
                        ex.id, "system", None, {"reason": reason})
        await db.commit()
    return ex


async def execution_is_orphaned(db: AsyncSession, ex: WorkflowExecution,
                                grace_seconds: float = 30.0) -> bool:
    """执行实例是否"没人管"（重启后残留 / 后台调度丢失）。

    判断依据：状态还在 RUNNING/PENDING、没有 worker 持有租约、且已过宽限期。
    用于服务重启后惰性恢复 —— 看板轮询（GET /executions/{id}）时把孤儿实例
    重新踢起来，而不是让它们永远停在"执行中"。
    """
    from app.task_engine import lock_is_held

    if ex.status not in (RunStatus.RUNNING, RunStatus.PENDING):
        return False
    if await lock_is_held(db, f"workflow:{ex.id}"):
        return False
    created = ex.created_at
    if created is None:
        return True
    if created.tzinfo is not None:
        created = created.replace(tzinfo=None)
    return (datetime.utcnow() - created).total_seconds() > grace_seconds


async def start_workflow(db: AsyncSession, workflow_id: str, started_by: str,
                         project_id: str | None = None,
                         input_data: dict | None = None) -> WorkflowExecution:
    # 初始状态 PENDING：真正的"开跑"由 advance_execution 用 CAS 抢 PENDING→RUNNING，
    # 这样"提交后台任务"与"后台开始执行"之间没有可被重复利用的窗口。
    ex = WorkflowExecution(workflow_id=workflow_id, project_id=project_id,
                           status=RunStatus.PENDING, input_data=input_data or {},
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
