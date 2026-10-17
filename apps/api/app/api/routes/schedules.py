"""Automation Scheduler API (P2-15)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.scheduler import (AutomationRun, AutomationSchedule,
                                  ScheduleStatus, compute_next_run)
from app.models.user import User
from app.scheduler_engine import run_due_schedules

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str
    trigger_type: str            # interval / daily
    seconds: int | None = None   # interval 用
    time: str | None = None      # daily 用 HH:MM
    action_type: str             # snapshot_performance / send_message / start_workflow
    action_params: dict | None = None
    attachments: list[dict] | None = None  # 📎 附件口


def _out(s: AutomationSchedule):
    return {"id": s.id, "name": s.name, "trigger_type": s.trigger_type,
            "trigger_config": s.trigger_config, "action_type": s.action_type,
            "action_params": s.action_params, "status": s.status,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "run_count": s.run_count, "attachments": s.attachments or []}


@router.post("")
async def create_schedule(
    data: ScheduleIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建调度（创建后立即计算 next_run_at）。"""
    config = {}
    if data.trigger_type == "interval":
        if not data.seconds or data.seconds < 10:
            raise HTTPException(400, "interval 需要 seconds >= 10")
        config = {"seconds": data.seconds}
    elif data.trigger_type == "daily":
        if not data.time:
            raise HTTPException(400, "daily 需要 time=HH:MM")
        config = {"time": data.time}
    else:
        raise HTTPException(400, "trigger_type 必须是 interval/daily")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sched = AutomationSchedule(
        id=str(uuid.uuid4()), company_id=company_id, name=data.name,
        trigger_type=data.trigger_type, trigger_config=config,
        action_type=data.action_type, action_params=data.action_params,
        attachments=data.attachments or [],
        created_by=user.id)
    sched.next_run_at = compute_next_run(data.trigger_type, config, now)
    # interval 类型的首个触发立即生效（方便测试）
    if data.trigger_type == "interval" and data.seconds and data.seconds <= 30:
        sched.next_run_at = now
    db.add(sched)
    await db.commit()
    return _out(sched)


@router.get("")
async def list_schedules(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(AutomationSchedule).where(
        AutomationSchedule.company_id == company_id).order_by(
        AutomationSchedule.created_at.desc()))).scalars().all()
    return [_out(s) for s in rows]


@router.post("/{sid}/pause")
async def pause_schedule(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sched = await db.get(AutomationSchedule, sid)
    if not sched:
        raise HTTPException(404, "not found")
    # 归属校验：与 archive 一致，防止跨公司暂停他人的调度
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, sched.company_id)
    sched.status = "PAUSED"
    await db.commit()
    return {"id": sid, "status": "PAUSED"}


@router.post("/{sid}/resume")
async def resume_schedule(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sched = await db.get(AutomationSchedule, sid)
    if not sched:
        raise HTTPException(404, "not found")
    # 归属校验：与 archive 一致，防止跨公司恢复他人的调度
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, sched.company_id)
    sched.status = "ACTIVE"
    sched.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"id": sid, "status": "ACTIVE"}


@router.delete("/{sid}")
async def archive_schedule(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sched = await db.get(AutomationSchedule, sid)
    if not sched:
        raise HTTPException(404, "not found")
    # 归属校验：原先按 id 就归档，任何登录用户都能停掉别家公司的定时任务
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, sched.company_id)
    sched.status = "ARCHIVED"
    await db.commit()
    return {"id": sid, "status": "ARCHIVED"}


@router.get("/{sid}/runs")
async def list_runs(
    sid: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(AutomationRun).where(
        AutomationRun.schedule_id == sid).order_by(
        AutomationRun.started_at.desc()).limit(limit))
    runs = q.scalars().all()
    return [{"id": r.id, "status": r.status, "result": r.result,
             "error": r.error,
             "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in runs]


@router.post("/tick")
async def tick(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发一次调度扫描（测试/调试用；后台循环也在跑）。"""
    n = await run_due_schedules(db)
    return {"executed": n}
