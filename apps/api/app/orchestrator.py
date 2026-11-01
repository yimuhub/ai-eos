"""旧编排入口（P1-10）→ CEO 内核的薄壳。

第一版 `orchestrate` 有三个硬伤：子任务上限写死 5 个、子任务执行时不带父目标、
全量 `asyncio.gather` 不看供应商并发。现在统一由 `app.ceo_engine` 承担，
本模块只保留旧签名与旧端点行为，避免系统里同时存在两套「CEO」。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.ceo_engine import (CeoError, create_mission, launch_mission,
                            run_mission)
from app.models.agent import Agent
from app.models.task import Task


async def orchestrate(db: AsyncSession, parent: Task, coordinator: Agent) -> Task:
    """兼容旧 /orchestrate：拆解 → 执行 → 汇总，同步跑完。

    调用方仍按老方式读取 ``parent.result``；父任务的状态迁移由内核在收尾时
    完成（COMPLETED / FAILED），路由不需要再补一刀。
    """
    try:
        preview = await create_mission(
            db, company_id=parent.company_id, user_id=parent.created_by,
            instruction=parent.description or parent.title,
            title=parent.title,
            coordinator_agent_id=str(coordinator.id),
            parent_task_id=parent.id)
        await launch_mission(db, preview["mission_id"], submit=False)
        await run_mission(db, preview["mission_id"])
    except CeoError as exc:
        raise RuntimeError(exc.message) from exc
    await db.refresh(parent)
    return parent
