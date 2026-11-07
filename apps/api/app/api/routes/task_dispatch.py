"""TaskOptionConfig 任务调度模块。

多目标分发(single/multi/org/company) · 动态优先级 · 全局中断。
复用既有能力：Task 模型/状态机(task_engine)、后台执行(_run_in_background)、
automation 紧急停止语义；本模块只做参数组装与批量编排，不重写底层。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, assert_company_owned,
                          get_current_user)
from app.core.permissions import Capability
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Department
from app.models.task import Task, TaskPriority, TaskStatus
from app.services.event_system import log_event

router = APIRouter(prefix="/task-dispatch", tags=["task-dispatch"])

PRIORITY_ZH = {"LOW": "低级", "NORMAL": "普通级", "HIGH": "优先级", "URGENT": "紧急级"}


class DispatchIn(BaseModel):
    company_id: str
    title: str
    description: str | None = None
    target_type: str = "single_agent"  # single_agent|multi_agent_list|agent_org|whole_company
    agent_ids: list[str] = []          # single_agent / multi_agent_list 用
    department_ids: list[str] = []     # agent_org 用（支持多部门）
    priority: str = "NORMAL"           # LOW|NORMAL|HIGH|URGENT
    # 分发即启动：队满时未启动的保留 auto_start 标记，由调度循环在 worker
    # 空闲后拉起（见 app/task_runner.py）。默认开工——"派活"不启动等于
    # 往待办里扔死行，用户还要再逐条点一次启动。
    auto_start: bool = True


async def _resolve_targets_async(db: AsyncSession, data: DispatchIn) -> list[Agent]:
    # 所有分支都必须带上 company_id 过滤：只校验"agent 存在"会允许把任务
    # 派给别家公司的员工（跨租户写入），department_ids 同理。
    if data.target_type in ("single_agent", "multi_agent_list"):
        ids = data.agent_ids[:1] if data.target_type == "single_agent" else data.agent_ids
        if not ids:
            raise HTTPException(400, "未指定执行 Agent")
        q = await db.execute(select(Agent).where(
            Agent.id.in_(ids), Agent.company_id == data.company_id))
        found = list(q.scalars().all())
        if len(found) != len(set(ids)):
            raise HTTPException(404, "部分执行 Agent 不存在")
        return found
    if data.target_type == "agent_org":
        if not data.department_ids:
            raise HTTPException(400, "未指定部门")
        q = await db.execute(select(Agent).where(
            Agent.department_id.in_(data.department_ids),
            Agent.company_id == data.company_id))
        found = list(q.scalars().all())
        if not found:
            raise HTTPException(400, "所选部门下没有员工")
        return found
    if data.target_type == "whole_company":
        q = await db.execute(select(Agent).where(
            Agent.company_id == data.company_id,
            Agent.status != "DISABLED"))
        return list(q.scalars().all())
    raise HTTPException(400, f"未知 target_type: {data.target_type}")


@router.get("/options")
async def dispatch_options(company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """UI 数据源：可选目标 + 优先级枚举 + 各目标人数预览。"""
    await assert_company_owned(db, user.id, company_id)

    depts = (await db.execute(select(Department).where(
        Department.company_id == company_id))).scalars().all()
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()

    dept_options = [{
        "id": d.id, "name": d.name,
        "agent_count": sum(1 for a in agents if a.department_id == d.id),
    } for d in depts]

    return {
        "priorities": [{"value": k, "label": v} for k, v in PRIORITY_ZH.items()],
        "target_types": [
            {"value": "single_agent", "label": "指定单个 Agent"},
            {"value": "multi_agent_list", "label": "多选一批 Agent"},
            {"value": "agent_org", "label": "指定组织单元"},
            {"value": "whole_company",
             "label": f"全域全员（{len(agents)} 名）"},
        ],
        "agents": [{"id": a.id, "name": a.name, "role": a.role,
                    "department_id": a.department_id} for a in agents],
        "departments": dept_options,
    }


@router.post("/dispatch", status_code=201)
async def dispatch_tasks(data: DispatchIn,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """按目标创建正式 Task 实例（每名执行 Agent 一条），绑定 priority。"""
    # 派活 = 日常写操作（OPERATE）
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.OPERATE)
    try:
        prio = TaskPriority(data.priority)
    except ValueError:
        raise HTTPException(400, f"priority 必须是 {list(PRIORITY_ZH)}")

    targets = await _resolve_targets_async(db, data)
    created = []
    for agent in targets:
        t = Task(company_id=data.company_id, title=data.title,
                 description=data.description, status=TaskStatus.TODO,
                 priority=prio, assigned_agent_id=agent.id,
                 created_by=user.id, auto_start=data.auto_start)
        db.add(t)
        created.append((t, agent))
    await db.flush()

    # 关联调度事件（含目标概要）
    await log_event(db, "task.dispatched", "task", created[0][0].id if created else None,
                    "user", user.id,
                    {"title": data.title, "target_type": data.target_type,
                     "targets": [a.name for _, a in created],
                     "priority": data.priority, "auto_start": data.auto_start})
    await db.commit()

    task_ids = [t.id for t, _ in created]

    # 一键开工：提交后台执行。队满的留在队列里，由调度循环接手；
    # 这样"派给全公司 20 人"不会被 8 个 worker 的瞬时容量卡成 12 条僵尸任务。
    if data.auto_start and task_ids:
        from app.task_runner import dispatch_auto_queue
        await dispatch_auto_queue(db, limit=len(task_ids) + 1)

    status_rows = (await db.execute(
        select(Task.id, Task.status).where(Task.id.in_(task_ids)))).all()
    status_of = {r[0]: r[1].value for r in status_rows}
    started = sum(1 for s in status_of.values() if s == "RUNNING")

    return {
        "created_count": len(created),
        "started_count": started,
        "queued_count": len(created) - started,
        "tasks": [{"task_id": t.id, "agent_id": a.id, "agent_name": a.name,
                   "priority": t.priority.value,
                   "status": status_of.get(t.id, "TODO")}
                  for t, a in created],
    }


class StartTasksIn(BaseModel):
    company_id: str
    task_ids: list[str]


@router.post("/start")
async def start_tasks(data: StartTasksIn,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """批量启动选中的待办任务。

    只处理 TODO：PAUSED 要走 /tasks/{id}/resume（恢复检查点），FAILED 要走
    /tasks/{id}/retry（重试语义不同）。逐条回报原因，一条失败不影响其余。
    """
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.OPERATE)
    from app.task_runner import start_task as _start

    started, queued, skipped = [], [], []
    for tid in data.task_ids:
        task = await db.get(Task, tid)
        if task is None or task.company_id != data.company_id:
            skipped.append({"task_id": tid, "reason": "不存在或无权限"})
            continue
        if task.status != TaskStatus.TODO:
            skipped.append({"task_id": tid,
                            "reason": f"状态 {task.status.value} 不能启动"})
            continue
        result = await _start(db, task, actor_type="user", actor_id=user.id)
        if result["started"]:
            started.append(tid)
        elif result["reason"] == "queue_full":
            # 队列满不是失败：任务退回 TODO 且保留自动拉起标记
            queued.append(tid)
        else:
            skipped.append({"task_id": tid, "reason": result["reason"]})

    return {"started_count": len(started), "queued_count": len(queued),
            "started": started, "queued": queued, "skipped": skipped}


class PriorityUpdateIn(BaseModel):
    priority: str


@router.patch("/running/priority")
async def update_running_priority(data: PriorityUpdateIn,
                                  company_id: str = Query(...),
                                  task_ids: list[str] | None = Query(None),
                                  db: AsyncSession = Depends(get_db),
                                  user=Depends(get_current_user)):
    """动态调整 RUNNING（及排队中）任务的优先级——调度器即时响应权重变化。"""
    # 批量改全公司任务优先级会改变调度顺序，属于公司级配置（MANAGE）
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    try:
        prio = TaskPriority(data.priority)
    except ValueError:
        raise HTTPException(400, f"priority 必须是 {list(PRIORITY_ZH)}")

    q = await db.execute(select(Task).where(Task.company_id == company_id))
    running = [t for t in q.scalars()
               if t.status in (TaskStatus.RUNNING, TaskStatus.TODO,
                               TaskStatus.PLANNING, TaskStatus.WAITING)
               and (not task_ids or t.id in task_ids)]
    changed = []
    for t in running:
        old = t.priority.value
        t.priority = prio
        changed.append({"task_id": t.id, "title": t.title,
                        "from": old, "to": prio.value})
    await log_event(db, "task.priority_changed", "task", None, "user", user.id,
                    {"count": len(changed), "new_priority": data.priority})
    await db.commit()
    return {"updated": len(changed), "changes": changed}


@router.post("/global-interrupt")
async def global_interrupt(company_id: str = Query(...),
                           reason: str | None = Query(None),
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """全局任务中断：向所有 RUNNING 任务发停止信号 → status=interrupted，保留记录并审计。

    复用 task_engine 的取消语义与 automation emergency-stop 模式。
    """
    from sqlalchemy import or_

    # 全局急停 = 影响面最大的一档（DESTRUCTIVE）
    await assert_company_capability(db, user.id, company_id,
                                    Capability.DESTRUCTIVE)

    q = await db.execute(select(Task).where(
        Task.company_id == company_id,
        or_(Task.status == TaskStatus.RUNNING, Task.status == TaskStatus.WAITING)))
    running = list(q.scalars().all())

    interrupted = []
    for t in running:
        prev = t.status.value if hasattr(t.status, "value") else str(t.status)
        t.status = TaskStatus.CANCELLED
        interrupted.append({"task_id": t.id, "title": t.title, "was": prev})
        # 同步终止其执行记录
        await db.execute(text(
            "UPDATE agent_executions SET status='cancelled' "
            "WHERE task_id=:tid AND status IN ('running','paused')"),
            {"tid": t.id})

    await log_event(db, "task.global_interrupt", "company", company_id,
                    "user", user.id,
                    {"count": len(interrupted), "reason": reason or "manual"})
    await db.commit()
    return {"interrupted_count": len(interrupted), "tasks": interrupted}


class DeleteTasksIn(BaseModel):
    task_ids: list[str]


@router.post("/delete")
async def delete_tasks(data: DeleteTasksIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """彻底删除任务：任务本体 + 执行记录 + 检查点 + 关联记忆/成果引用 全部清除。"""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    user_id = user.id
    # 权限预校验：任务彻底删除（含执行记录/检查点）不可逆 → DESTRUCTIVE
    owned: set[str] = set()
    for tid in data.task_ids:
        row = await db.get(Task, tid)
        if row is None:
            continue
        try:
            await assert_company_capability(db, user_id, row.company_id,
                                            Capability.DESTRUCTIVE)
        except HTTPException:
            continue
        owned.add(tid)

    deleted, failed = [], []
    for tid in data.task_ids:
        if tid not in owned:
            failed.append({"id": tid, "reason": "不存在或无权限"})
            continue
        t = (await db.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not t:
            failed.append({"id": tid, "reason": "不存在"})
            continue
        title = t.title  # 提前取值：删除/回滚后 ORM 对象过期，避免再触发懒加载
        try:
            async with db.begin_nested():
                # 跨方言：IN 子查询在 PostgreSQL / MySQL / SQLite 上都合法
                await db.execute(text(
                    "DELETE FROM execution_checkpoints WHERE execution_id IN "
                    "(SELECT id FROM agent_executions WHERE task_id = :tid)"),
                    {"tid": tid})
                await db.execute(text(
                    "DELETE FROM agent_executions WHERE task_id = :tid"), {"tid": tid})
                await db.execute(text(
                    "DELETE FROM task_events WHERE task_id = :tid"), {"tid": tid})
                # 该任务产出的记忆（context_ref 指向此任务的）一并删除
                await db.execute(text(
                    "DELETE FROM agent_memories WHERE context_ref = :tid"), {"tid": tid})
                # 子任务（parent_task_id 引用，递归一层足够）
                await db.execute(text(
                    "DELETE FROM tasks WHERE parent_task_id = :tid"), {"tid": tid})
                # 人工介入记录
                await db.execute(text(
                    "DELETE FROM interventions WHERE task_id = :tid"), {"tid": tid})
                # 任务产出的资产解绑（保留文件）
                await db.execute(text(
                    "UPDATE assets SET task_id = NULL WHERE task_id = :tid"), {"tid": tid})
                # 会议行动项解绑（保留记录）
                await db.execute(text(
                    "UPDATE meeting_action_items SET task_id = NULL WHERE task_id = :tid"),
                    {"tid": tid})
                await db.delete(t)
                # 显式 flush：让本条的约束错误在 savepoint 内就暴露，
                # 而不是拖到整批 commit 才一起失败。
                await db.flush()
            deleted.append({"id": tid, "title": title})
        except (IntegrityError, DBAPIError) as e:
            # savepoint 已由 begin_nested 上下文回滚；不要再 db.rollback()，
            # 否则会把此前已成功删除的任务一起撤销，却仍报告为成功。
            failed.append({"id": tid,
                           "reason": f"约束冲突: {str(getattr(e, 'orig', e))[:100]}"})
            db.expire_all()

    if deleted:
        await log_event(db, "task.deleted", "task", None, "user", user_id,
                        {"count": len(deleted),
                         "titles": [x["title"][:50] for x in deleted]})
    try:
        await db.commit()
    except Exception as e:
        # 最终提交失败时整批都未落库，不能继续把它们报成成功
        await db.rollback()
        failed.extend({"id": x["id"], "reason": f"提交失败: {str(e)[:80]}"}
                      for x in deleted)
        deleted = []

    return {"deleted_count": len(deleted), "deleted": deleted, "failed": failed}
