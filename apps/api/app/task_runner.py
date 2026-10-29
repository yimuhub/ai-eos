"""任务启动与自动排队：路由 / 调度循环 / 批量派活共用的唯一入口。

为什么单独拆出来：此前能把任务从 TODO 变成 RUNNING 的只有
`POST /tasks/{id}/start` 一个路由，批量派活、指挥台、自动化都只建
TODO 行，于是"一键派给全公司"实际产出的是 N 条永远不动的待办。

队列语义（对齐 Lindy 的"排队而不是丢弃"）：
* 启动一次 = CAS(TODO→RUNNING) + 投递后台执行；CAS 保证双击只跑一次；
* 投递时 worker 池可能已满（`QUEUE_FULL`）——此时把任务**退回 TODO**
  并保留 `auto_start` 标记，由调度循环下一拍再拉起，绝不留下
  "RUNNING 但没有 worker 在跑"的僵尸行；
* 自动排队只在有 `auto_start` 标记的任务上进行，用户手工建的任务
  不会被系统擅自启动。
"""
from __future__ import annotations

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.background_jobs as background_jobs
from app.core.logging_config import get_logger
from app.models.task import Task, TaskPriority, TaskStatus
from app.task_engine import IllegalTransition, transition

logger = get_logger(__name__)

# 每拍最多拉起多少个：与后台 worker 数同量级即可。更大的批次没有意义，
# 多出来的会在下一次扫描继续启动。
AUTO_DISPATCH_BATCH = 8


async def start_task(db: AsyncSession, task: Task, actor_type: str = "system",
                     actor_id: str | None = None) -> dict:
    """启动一个任务。返回 {started, reason}，不抛异常（让批量调用继续）。"""
    if not task.assigned_agent_id:
        return {"started": False, "reason": "no_agent"}

    try:
        await transition(db, task, TaskStatus.RUNNING, actor_type, actor_id)
    except IllegalTransition as exc:
        return {"started": False, "reason": str(exc)}

    # 启动即消费掉排队意图，避免任务失败后被调度循环反复重新拉起
    await db.execute(update(Task).where(Task.id == task.id)
                     .values(auto_start=False)
                     .execution_options(synchronize_session=False))

    # 懒导入：路由模块在应用启动时已加载，这里只为避免 import 环
    from app.api.routes.tasks import _run_in_background
    submitted = _run_in_background(task.id, task.assigned_agent_id)
    if submitted is background_jobs.QUEUE_FULL:
        # worker 全忙：回退状态，下一次扫描再启动（不丢任务、不假运行）
        await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == TaskStatus.RUNNING)
            .values(status=TaskStatus.TODO, auto_start=True)
            .execution_options(synchronize_session=False))
        await db.commit()
        logger.info("任务排队已满，退回待办等待下一拍 task=%s", task.id)
        return {"started": False, "reason": "queue_full"}

    await db.commit()
    return {"started": True, "reason": ""}


async def dispatch_auto_queue(db: AsyncSession, limit: int | None = None) -> int:
    """把带 auto_start 标记的待办任务按优先级拉起，直到 worker 排满。

    返回本次真正启动的任务数。调度循环每 10 秒调用一次。
    """
    limit = limit or AUTO_DISPATCH_BATCH
    prio_order = case(
        (Task.priority == TaskPriority.URGENT, 0),
        (Task.priority == TaskPriority.HIGH, 1),
        (Task.priority == TaskPriority.NORMAL, 2),
        else_=3)
    ids = (await db.execute(
        select(Task.id).where(
            Task.status == TaskStatus.TODO,
            Task.auto_start.is_(True))
        .order_by(prio_order, Task.created_at)
        .limit(limit))).scalars().all()
    if not ids:
        return 0

    # 结束只读快照：SQLite/WAL 下"读后升级写"容易撞 SNAPSHOT 冲突
    await db.commit()

    started = 0
    for tid in ids:
        task = await db.get(Task, tid)
        if not task or task.status != TaskStatus.TODO:
            continue
        r = await start_task(db, task, actor_type="system")
        if r["started"]:
            started += 1
        elif r["reason"] == "queue_full":
            break  # 队列已满，剩下的下一拍再来
    return started
