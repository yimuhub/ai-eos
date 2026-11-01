"""Advanced Automation API (P3-12)."""
import copy
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation_engine import run_automation
from app.models.automation import AutomationDefinition, AutomationRunRecord
from app.models.user import User
from app.api.deps import assert_company_owned, get_current_user, get_db
from app.core.permissions import require_company_scope

router = APIRouter(prefix="/automations", tags=["automations"],
                   dependencies=[Depends(require_company_scope())])


async def _owned_automation(db: AsyncSession, user_id: str, aid: str):
    """按 {aid} 取定义并校验公司归属。

    这些端点的路径里没有 company_id，router 级 require_company_scope 会直接
    跳过——不在这里比对归属，任何登录用户知道 id 就能跑/停/归档别家的自动化。
    """
    d = await db.get(AutomationDefinition, aid)
    if not d:
        raise HTTPException(404, "not found")
    await assert_company_owned(db, user_id, d.company_id)
    return d
class DefinitionIn(BaseModel):
    name: str
    trigger_type: str = "MANUAL"
    trigger_config: dict | None = None
    steps: list[dict]
    mode: str = "TEST"          # TEST / SIMULATION / PRODUCTION
    max_runs_per_day: int = 10
    attachments: list[dict] | None = None  # 📎 附件口


@router.post("", status_code=201)
async def create_automation(
    data: DefinitionIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.trigger_type not in ("TIME", "EVENT", "FILE", "PROJECT", "TASK",
                                  "AGENT", "WEBHOOK", "HUMAN", "MANUAL"):
        raise HTTPException(400, "invalid trigger_type")
    if data.mode not in ("TEST", "SIMULATION", "PRODUCTION"):
        raise HTTPException(400, "mode must be TEST/SIMULATION/PRODUCTION")
    d = AutomationDefinition(id=str(__import__("uuid").uuid4()),
                             company_id=company_id, name=data.name,
                             trigger_type=data.trigger_type,
                             trigger_config=data.trigger_config,
                             steps=data.steps, mode=data.mode,
                             max_runs_per_day=data.max_runs_per_day,
                             attachments=data.attachments or [],
                             created_by=user.id)
    db.add(d)
    await db.commit()
    return {"id": d.id, "name": d.name, "mode": d.mode}


@router.get("")
async def list_automations(company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    q = await db.execute(select(AutomationDefinition).where(
        AutomationDefinition.company_id == company_id).order_by(
        AutomationDefinition.created_at.desc()))
    return [{"id": a.id, "name": a.name, "trigger_type": a.trigger_type,
             # 前端据此区分「任务模板」（kind=TASK_TEMPLATE）与普通规则
             "trigger_config": a.trigger_config or {},
             "mode": a.mode, "steps_count": len(a.steps or []),
             "status": a.status, "attachments": a.attachments or []} for a in q.scalars().all()]


@router.post("/{aid}/clone", status_code=201)
async def clone_automation(
    aid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """复制一条自动化定义。

    只复制**定义**（steps / trigger / 附件），绝不复制运行记录；
    副本强制降为 TEST 且初始 PAUSED，避免复制出一个立即生产的规则。
    """
    src = await _owned_automation(db, user.id, aid)
    base = (src.name or "自动化")[:196]
    d = AutomationDefinition(
        id=str(uuid.uuid4()), company_id=src.company_id,
        name=f"{base}（副本）",
        trigger_type=src.trigger_type,
        trigger_config=copy.deepcopy(src.trigger_config),
        steps=copy.deepcopy(src.steps or []),
        mode="TEST", status="PAUSED",
        max_runs_per_day=src.max_runs_per_day,
        attachments=copy.deepcopy(src.attachments or []),
        created_by=user.id)
    db.add(d)
    await db.commit()
    return {"id": d.id, "name": d.name, "mode": d.mode, "status": d.status,
            "steps_count": len(d.steps or [])}


@router.post("/{aid}/run")
async def run(
    aid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """执行一次自动化（按定义的 mode：TEST=DryRun）。"""
    d = await _owned_automation(db, user.id, aid)
    if d.status == "ARCHIVED":
        raise HTTPException(409, "已归档")
    run = await run_automation(db, d)
    return {"run_id": run.id, "status": run.status, "mode": run.mode,
            "steps": run.step_results, "error": run.error}


@router.patch("/{aid}")
async def update_automation(
    aid: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """统一状态切换：pause / resume / archive。"""
    d = await _owned_automation(db, user.id, aid)
    action = (payload or {}).get("action", "")
    if action == "pause":
        d.status = "PAUSED"
    elif action == "resume":
        d.status = "ENABLED"
    elif action == "archive":
        d.status = "ARCHIVED"
    else:
        raise HTTPException(400, f"invalid action={action}")
    await db.commit()
    return {"id": aid, "status": d.status}


@router.post("/{aid}/enable")
async def enable(aid: str, db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    d = await _owned_automation(db, user.id, aid)
    d.status = "ENABLED"
    await db.commit()
    return {"id": aid, "status": "ENABLED"}


@router.post("/runs/{rid}/approve-continue")
async def approve_continue(rid: str, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """审批等待中的自动化：从断点（wait_approval 步骤）继续执行剩余步骤。"""
    run = await db.get(AutomationRunRecord, rid)
    if not run:
        raise HTTPException(404, "not found")
    definition = await db.get(AutomationDefinition, run.definition_id)
    if not definition:
        raise HTTPException(404, "definition missing")
    await assert_company_owned(db, user.id, definition.company_id)
    if run.status != "WAITING_APPROVAL":
        raise HTTPException(409, f"status={run.status}")

    # 记录人工批准，然后从断点恢复执行
    results = list(run.step_results or [])
    for r in results:
        if r.get("status") == "WAITING_APPROVAL":
            r["status"] = "APPROVED"
            r["approved_by"] = user.id
    run.step_results = results
    await db.commit()

    done = max((s.get("seq") or 0) for s in results if s.get("seq")) if results else 0
    resumed = await run_automation(db, definition,
                                   resume_run=run)
    return {"run_id": rid, "status": resumed.status,
            "resumed_from_step": done + 1,
            "steps": resumed.step_results}


@router.post("/emergency-stop")
async def emergency_stop(company_id: str = Query(...),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """紧急停止：本公司的自动化立即中止。

    修复两处真实缺陷：
    1. **不校验归属**——任何登录用户传一个 company_id 就能触发急停；
    2. **不按公司过滤**——原查询直接捞全部 RUNNING/WAITING_APPROVAL 运行记录，
       结果是 A 公司点急停会把 B 公司正在跑的自动化一并中止。
    """
    from app.core.permissions import Capability, ensure_capability
    await ensure_capability(db, user.id, company_id, Capability.DESTRUCTIVE)

    q = await db.execute(
        select(AutomationRunRecord)
        .join(AutomationDefinition,
              AutomationDefinition.id == AutomationRunRecord.definition_id)
        .where(AutomationDefinition.company_id == company_id,
               AutomationRunRecord.status.in_(("RUNNING", "WAITING_APPROVAL"))))
    stopped = 0
    for r in q.scalars().all():
        r.status = "ABORTED"
        stopped += 1
    # 同时停用所有启用的自动化
    dq = await db.execute(select(AutomationDefinition).where(
        AutomationDefinition.company_id == company_id,
        AutomationDefinition.status == "ENABLED"))
    disabled = 0
    for d in dq.scalars().all():
        d.status = "PAUSED"
        disabled += 1
    await db.commit()
    return {"stopped_runs": stopped, "disabled_automations": disabled}


@router.get("/{aid}/runs")
async def run_history(aid: str, limit: int = Query(20, ge=1, le=100),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    # 路径里没有 company_id，router 级 scope 会跳过；先校验定义归属，
    # 否则知道别家 aid 就能读到其运行历史（与 /run、/stop 同一道门）。
    await _owned_automation(db, user.id, aid)
    q = await db.execute(select(AutomationRunRecord).where(
        AutomationRunRecord.definition_id == aid).order_by(
        AutomationRunRecord.started_at.desc()).limit(limit))
    return [{"id": r.id, "status": r.status, "mode": r.mode,
             "steps": r.step_results, "error": r.error} for r in q.scalars().all()]
