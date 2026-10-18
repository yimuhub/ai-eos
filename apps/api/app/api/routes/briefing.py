"""CEO 晨间简报（V5.5 P0）：老板打开 App 第一眼看到的公司动态。

学 Tycoon/Polsia 的一人公司共识交互：昨日完成 / 今日计划 / 需要你决策的三件事。
数据全部来自现有表（tasks / automation_runs / approval_requests /
meetings / token_usages），零新表。GET /briefing 鉴权，返回用户首家公司视角。
"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, join, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.company import Company
from app.models.intervention import ApprovalRequest
from app.models.meeting import Meeting, MeetingStatus
from app.models.scheduler import AutomationRun, AutomationSchedule
from app.models.task import Task, TaskStatus
from app.models.token_usage import TokenUsage
from app.models.user import User

router = APIRouter(prefix="/briefing", tags=["briefing"])

# 高风险：跑超过 2 小时的 RUNNING 视为卡住，进"需要你决策"
STUCK_HOURS = 2


@router.get("")
async def get_briefing(
    company_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if company_id:
        company = await db.get(Company, company_id)
        if not company or (company.owner_id != user.id and user.id != company.created_by):
            raise HTTPException(404, "公司不存在")
    else:
        rows = (await db.execute(
            select(Company).where(Company.owner_id == user.id).limit(1)
        )).scalars().all()
        company = rows[0] if rows else None
    if company is None:
        return {"has_company": False, "greeting": "", "yesterday": {}, "today": {},
                "needs_you": {"count": 0, "items": []}, "cost_today": {}}
    return await build_briefing(db, company)


async def build_briefing(db: AsyncSession, company: Company) -> dict:
    """CEO 晨间简报数据（路由与调度器推送共用）。"""
    cid = company.id
    now = datetime.now()
    yesterday_from = now - timedelta(hours=24)
    today_from = datetime(now.year, now.month, now.day)

    # ---- 昨日：完成任务 + 自动化执行（经 schedule join 到公司）----
    done_tasks = (await db.execute(
        select(Task.title, Task.updated_at).where(
            Task.company_id == cid, Task.status == TaskStatus.COMPLETED,
            Task.updated_at >= yesterday_from).order_by(Task.updated_at.desc()).limit(5)
    )).all()
    run_base = join(AutomationRun, AutomationSchedule,
                    AutomationRun.schedule_id == AutomationSchedule.id)
    run_ok = (await db.execute(
        select(func.count()).select_from(run_base).where(
            AutomationSchedule.company_id == cid,
            AutomationRun.started_at >= yesterday_from,
            AutomationRun.status == "SUCCESS"))).scalar() or 0
    run_bad = (await db.execute(
        select(func.count()).select_from(run_base).where(
            AutomationSchedule.company_id == cid,
            AutomationRun.started_at >= yesterday_from,
            AutomationRun.status == "FAILED"))).scalar() or 0

    # ---- 今日：进行中/待办任务 + 待开会议 + 今日自动化运行 ----
    active_tasks = (await db.execute(
        select(Task.title, Task.status).where(
            Task.company_id == cid,
            Task.status.in_([TaskStatus.RUNNING, TaskStatus.WAITING,
                            TaskStatus.REVIEW, TaskStatus.PLANNING])).limit(5)
    )).all()
    todo_count = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.company_id == cid, Task.status == TaskStatus.TODO))).scalar() or 0
    meetings = (await db.execute(
        select(Meeting.title, Meeting.status).where(
            Meeting.company_id == cid,
            Meeting.status == MeetingStatus.SCHEDULED).limit(3)
    )).all()
    sched_today = (await db.execute(
        select(func.count()).select_from(run_base).where(
            AutomationSchedule.company_id == cid,
            AutomationRun.started_at >= today_from,
            AutomationRun.status == "SUCCESS"))).scalar() or 0

    # ---- 需要你决策：待审批 + 失败任务 + 卡住的执行 ----
    approvals = (await db.execute(
        select(ApprovalRequest.action_type, ApprovalRequest.reason,
               ApprovalRequest.created_at).where(
            ApprovalRequest.company_id == cid,
            ApprovalRequest.status == "PENDING").order_by(ApprovalRequest.created_at.desc())
    )).all()
    failed_tasks = (await db.execute(
        select(Task.title).where(
            Task.company_id == cid, Task.status == TaskStatus.FAILED).limit(3)
    )).all()
    stuck_tasks = (await db.execute(
        select(Task.title).where(
            Task.company_id == cid, Task.status == TaskStatus.RUNNING,
            Task.updated_at <= now - timedelta(hours=STUCK_HOURS)).limit(3)
    )).all()

    items = []
    for a in approvals[:3]:
        items.append({"kind": "审批", "text": a.reason or f"动作 {a.action_type} 待你批准",
                      "goto": "/intervention"})
    for t in failed_tasks:
        items.append({"kind": "失败任务", "text": f"「{t[0]}」执行失败，可重试或查看",
                      "goto": "/command"})
    for t in stuck_tasks:
        items.append({"kind": "疑似卡住", "text": f"「{t[0]}」运行超 {STUCK_HOURS}h 无进展，请检查",
                      "goto": "/command"})
    for m in meetings[:2]:
        items.append({"kind": "待开会议", "text": f"会议「{m.title}」待开始/进行中",
                      "goto": "/meetings"})

    # ---- 今日成本 ----
    cost_row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
               func.coalesce(func.sum(TokenUsage.total_tokens), 0)).where(
            TokenUsage.company_id == cid, TokenUsage.created_at >= today_from))).one()
    cost_today = {"cost": round(float(cost_row[0]), 2), "tokens": int(cost_row[1])}

    hour = now.hour
    greeting = "早上好" if hour < 12 else ("下午好" if hour < 18 else "晚上好")

    # V5.5 A2：预算软提醒（≥80% 黄色预警，到顶另见 paused）
    budget_alert: dict | None = None
    try:
        from app.models.budget import TokenBudget
        bts = (await db.execute(
            select(TokenBudget).where(
                TokenBudget.company_id == cid,
                TokenBudget.enabled == True,  # noqa: E712
                or_(TokenBudget.scope_type == "company",
                    TokenBudget.scope_type == "department")))).scalars().all()
        for b in bts:
            uq = select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
                        func.coalesce(func.sum(TokenUsage.total_cost), 0.0)).where(
                TokenUsage.company_id == cid)
            if b.scope_type == "agent" and b.scope_id:
                uq = uq.where(TokenUsage.agent_id == b.scope_id)
            elif b.scope_type == "department" and b.scope_id:
                uq = uq.where(TokenUsage.department_id == b.scope_id)
            if b.period == "day":
                uq = uq.where(TokenUsage.created_at >= today_from)
            trow = (await db.execute(uq)).one()
            tokens, cost = int(trow[0]), float(trow[1])
            if b.limit_tokens and tokens >= b.limit_tokens * 0.8:
                pct = round(tokens * 100 / b.limit_tokens)
                budget_alert = {"pct": pct, "paused": b.paused,
                                "message": (f"预算{'已暂停' if b.paused else '即将用尽'}："
                                            f"已用 {tokens}/{b.limit_tokens} token（{pct}%）"
                                            f"，可到「Token 统计 → 预算上限」调高额度")}
                break
    except Exception:
        budget_alert = None

    return {
        "has_company": True,
        "company": {"id": cid, "name": company.name},
        "greeting": f"{greeting}，{company.name or '老板'}",
        "yesterday": {
            "completed_tasks": [{"title": t[0]} for t in done_tasks],
            "automation_ok": run_ok, "automation_failed": run_bad,
        },
        "today": {
            "active_tasks": [{"title": t[0], "status": t[1].value} for t in active_tasks],
            "todo_count": todo_count,
            "meetings": [{"title": m[0], "status": m[1].value} for m in meetings],
            "automation_runs_today": sched_today,
        },
        "needs_you": {"count": len(items), "items": items},
        "cost_today": cost_today,
        "budget_alert": budget_alert,
    }


@router.post("/push-schedule")
async def ensure_push_schedule(
    company_id: str = Query(...),
    time_str: str = Query("09:00"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一键创建/更新「每日简报推送」调度（A1）。

    到点由 scheduler 生成 BRIEFING_PUSH 事件进事件流，
    首页简报卡标注"最后一次推送时间"，浏览器可弹系统通知。
    """
    company = await db.get(Company, company_id)
    if not company or (company.owner_id != user.id and user.id != company.created_by):
        raise HTTPException(404, "公司不存在")
    hh, mm = time_str.split(":")
    sched = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == company_id,
            AutomationSchedule.action_type == "daily_briefing"))).scalars().first()
    if not sched:
        from app.models.scheduler import _now
        sched = AutomationSchedule(id=str(uuid.uuid4()),
                                    company_id=company_id,
                                    name="CEO 简报每日推送",
                                    trigger_type="daily",
                                    trigger_config={"time": f"{hh}:{mm}"},
                                    action_type="daily_briefing",
                                    status="ACTIVE",
                                    created_by=user.id,
                                    next_run_at=_now())
        db.add(sched)
    else:
        from app.models.scheduler import compute_next_run
        sched.trigger_config = {"time": f"{hh}:{mm}"}
        sched.next_run_at = compute_next_run("daily", sched.trigger_config, datetime.now())
    await db.commit()
    return {"schedule_id": sched.id, "time": f"{hh}:{mm}", "exists": bool(sched.id)}
