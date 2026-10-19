"""Token 预算上限 API（V5.5 P0 一人公司成本护栏）。

老板成本哲学：到顶就停，不加价。
- GET  /token-budgets?company_id=        公司全部预算 + 当前用量/占比
- POST /token-budgets                   建上限（scope: company|agent, period: day|total）
- PUT  /token-budgets/{id}              调整/启停（enabled/paused/调额立即生效）
- DELETE /token-budgets/{id}
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
from app.models.budget import TokenBudget
from app.models.token_usage import TokenUsage
from app.models.user import User

router = APIRouter(prefix="/token-budgets", tags=["token_budgets"])


class BudgetIn(BaseModel):
    scope_type: str = "company"   # company | agent
    scope_id: str | None = None
    limit_tokens: int = 0
    limit_cost: float = 0.0
    period: str = "day"           # day | total
    enabled: bool = True
    note: str | None = None


class BudgetPatch(BaseModel):
    limit_tokens: int | None = None
    limit_cost: float | None = None
    period: str | None = None
    enabled: bool | None = None
    paused: bool | None = None
    note: str | None = None


async def _usage_for_async(db: AsyncSession, b: TokenBudget) -> tuple[int, float]:
    q = select(
        func.coalesce(func.sum(TokenUsage.total_tokens), 0),
        func.coalesce(func.sum(TokenUsage.total_cost), 0.0),
    ).where(TokenUsage.company_id == b.company_id)
    if b.scope_type == "agent" and b.scope_id:
        q = q.where(TokenUsage.agent_id == b.scope_id)
    elif b.scope_type == "department" and b.scope_id:
        q = q.where(TokenUsage.department_id == b.scope_id)
    if b.period == "day":
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.where(TokenUsage.created_at >= start)
    row = (await db.execute(q)).one()
    return int(row[0]), float(row[1])


def _is_exceeded(b: TokenBudget, tokens: int, cost: float) -> bool:
    if b.limit_tokens and tokens >= b.limit_tokens:
        return True
    if b.limit_cost and cost >= b.limit_cost:
        return True
    return False


@router.get("")
async def list_budgets(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    rows = (await db.execute(select(TokenBudget).where(
        TokenBudget.company_id == company_id)
        .order_by(TokenBudget.created_at.desc()))).scalars().all()
    out = []
    for b in rows:
        d = b.to_dict()
        if not b.enabled:
            d["used_tokens"] = d["used_cost"] = None
            d["exceeded"] = False
            continue
        tokens, cost = await _usage_for_async(db, b)
        exceeded = _is_exceeded(b, tokens, cost)
        # V5.5 A2：占比（进度条）+ 软预警（≥80%）
        usage_pct = None
        if b.limit_tokens:
            usage_pct = round(tokens * 100 / b.limit_tokens)
        elif b.limit_cost:
            usage_pct = round(cost * 100 / b.limit_cost)
        d.update({"used_tokens": tokens, "used_cost": round(cost, 4),
                  "exceeded": exceeded, "usage_pct": usage_pct,
                  "warn": not exceeded and usage_pct is not None and usage_pct >= 80})
        # 自动挂起：用量到顶且未手动解除 → 标记 paused（前端显示"已暂停"）
        if not b.paused and _is_exceeded(b, tokens, cost):
            b.paused = True
            b.paused_reason = (f"用量到顶（{tokens}/{b.limit_tokens or '∞'} token，"
                               f"¥{cost:.2f}/¥{b.limit_cost or '∞'}），已自动暂停")
            await db.commit()
            d["paused"] = True
            d["paused_reason"] = b.paused_reason
        out.append(d)
    return {"budgets": out}


@router.post("")
async def create_budget(
    data: BudgetIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    if data.scope_type not in ("company", "agent", "department"):
        raise HTTPException(400, "scope_type 必须是 company、agent 或 department")
    if data.period not in ("day", "total"):
        raise HTTPException(400, "period 必须是 day 或 total")
    if data.limit_tokens <= 0 and data.limit_cost <= 0:
        raise HTTPException(400, "至少要设一个上限（tokens 或 cost）")
    b = TokenBudget(company_id=company_id, scope_type=data.scope_type,
                    scope_id=data.scope_id if data.scope_type in ("agent", "department") else None,
                    limit_tokens=data.limit_tokens, limit_cost=data.limit_cost,
                    period=data.period, enabled=data.enabled, note=data.note,
                    updated_by=user.id)
    db.add(b)
    await db.commit()
    return b.to_dict()


@router.put("/{budget_id}")
async def patch_budget(
    budget_id: str,
    data: BudgetPatch,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    b = await db.get(TokenBudget, budget_id)
    if not b or b.company_id != company_id:
        raise HTTPException(404, "预算不存在")
    for field in ("limit_tokens", "limit_cost", "period", "enabled",
                  "paused", "note"):
        val = getattr(data, field)
        if val is not None:
            setattr(b, field, val)
    # 老板主动解除暂停/调高上限 → 立即恢复（这是"到顶就停"的对侧开关）
    resumed_tasks: list[str] = []
    if data.enabled is True or data.paused is False or data.limit_tokens or data.limit_cost:
        was_paused = b.paused
        b.paused = False
        b.paused_reason = None
        # V5.6.1 tripped→resume：预算恢复后，自动把该范围内 PAUSED 的
        # AgentExecution 续跑（学 OpenAI SDK"加预算续跑"；老板有可视化确认点）
        if was_paused:
            from app.models.task import AgentExecution
            # 找范围内 PAUSED 执行：agent 级按 agent_id；department 级先查该部门
            # 下的 agent 集合；company 级全部
            agent_ids: list[str] | None = None
            if b.scope_type == "agent" and b.scope_id:
                agent_ids = [b.scope_id]
            elif b.scope_type == "department" and b.scope_id:
                from app.models.agent import Agent as _Agent
                agent_ids = [str(a.id) for a in (await db.execute(
                    select(_Agent).where(_Agent.department_id == b.scope_id)
                )).scalars().all()]
            q2 = select(AgentExecution).where(AgentExecution.status == "PAUSED")
            if agent_ids:
                q2 = q2.where(AgentExecution.agent_id.in_(agent_ids))
            execs = (await db.execute(q2)).scalars().all()
            for ex in execs:
                # 只恢复范围匹配的（company 级全恢复；agent/department 级按范围）
                if b.scope_type == "agent" and ex.agent_id != b.scope_id:
                    continue
                if b.scope_type == "department" and ex.department_id != b.scope_id:
                    continue
                try:
                    from app.agent_runtime import resume_execution
                    await resume_execution(db, ex)
                    resumed_tasks.append(ex.task_id or ex.id)
                except Exception:
                    continue
    b.updated_by = user.id
    await db.commit()
    return {**b.to_dict(),
            "resumed_executions": resumed_tasks,
            "note": (f"预算已恢复，{len(resumed_tasks)} 个挂起执行自动续跑"
                     if resumed_tasks else None)}


@router.delete("/{budget_id}")
async def delete_budget(
    budget_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    b = await db.get(TokenBudget, budget_id)
    if not b or b.company_id != company_id:
        raise HTTPException(404, "预算不存在")
    await db.delete(b)
    await db.commit()
    return {"deleted": budget_id}
