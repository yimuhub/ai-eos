"""Task state machine — illegal transitions are rejected; every change is logged."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskEvent, TaskEventType, TaskStatus
from app.services.event_system import log_event

# Allowed transitions (Task Status Machine)
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.WAITING, TaskStatus.PAUSED, TaskStatus.REVIEW,
                         TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.REVIEW: {TaskStatus.COMPLETED, TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.PLANNING, TaskStatus.CANCELLED},  # retry allowed
    TaskStatus.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


async def transition(
    db: AsyncSession,
    task: Task,
    new_status: TaskStatus,
    actor_type: str = "system",
    actor_id: str | None = None,
    payload: dict | None = None,
    commit: bool = True,
) -> Task:
    old = task.status
    if new_status not in TRANSITIONS.get(old, set()):
        raise IllegalTransition(f"Illegal transition {old.value} -> {new_status.value}")
    task.status = new_status
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.STATUS_CHANGED,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={"from": old.value, "to": new_status.value, **(payload or {})},
        )
    )
    await log_event(
        db, "task.status_changed", "task", task.id, actor_type, actor_id,
        {"from": old.value, "to": new_status.value},
    )
    if commit:
        await db.commit()
    return task


async def add_task_event(
    db: AsyncSession, task_id: str, event_type: TaskEventType,
    actor_type: str, actor_id: str | None = None, payload: dict | None = None,
):
    db.add(TaskEvent(task_id=task_id, event_type=event_type,
                     actor_type=actor_type, actor_id=actor_id, payload=payload or {}))
    await db.flush()
