"""任务完成 → 交付单（Artifact）自动登记。

为什么需要这一层：任务跑完只把产出写进 `tasks.result`，收件箱里这条任务
就只是"已完成"，永远不会出现在"待验收"；IN_REVIEW / APPROVED → 验收
（accepted_at）这条链路只有行业包铺设和手工建成果两个入口。结果就是
员工交活了，系统里却没有人能签收。

口径：
* 只有**有正文产出**的任务才生成交付单（空结果不伪造成果）；
* 一个任务最多一张交付单（重复完成/重试不重复生成）；
* 初始状态 IN_REVIEW：产出是员工交的，等老板审核/验收；
* 不 commit：与任务状态迁移同事务，要么一起落库，要么一起回滚。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.task import Task

_CHANGE_NOTE = "任务完成自动提交"


async def record_task_deliverable(db: AsyncSession, task: Task,
                                  *, change_note: str = _CHANGE_NOTE
                                  ) -> Artifact | None:
    """把任务产出登记成待验收成果；没有产出或已登记过则返回 None。"""
    content = (task.result or "").strip()
    if not content:
        return None

    existing = (await db.execute(
        select(Artifact.id).where(Artifact.task_id == task.id).limit(1)
    )).scalar_one_or_none()
    if existing:
        return None

    # created_by 存的是用户 id：验收通知按它发（见 artifacts.accept_artifact），
    # 写成 agent id 会撞 notifications.user_id 的外键。交活的人记在版本上。
    art = Artifact(
        id=str(uuid.uuid4()),
        company_id=task.company_id,
        project_id=task.project_id,
        name=(task.title or "任务交付物")[:300],
        artifact_type="document",
        current_version=1,
        status=ArtifactStatus.IN_REVIEW,
        created_by=task.created_by,
        task_id=task.id,
    )
    db.add(art)
    db.add(ArtifactVersion(
        id=str(uuid.uuid4()), artifact_id=art.id, version=1,
        content=content, change_note=change_note,
        status=ArtifactStatus.IN_REVIEW,
        submitted_by=task.assigned_agent_id))
    await db.flush()
    return art
