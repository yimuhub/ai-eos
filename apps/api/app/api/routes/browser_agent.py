"""Browser Agent API (P2-13).

租户边界：可访问公司集合由服务端从「owner 事实源 ∪ active 公司成员行」推导，
客户端传入的 company_id 只用于“选定其中一个”，不再被直接当作过滤条件。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.browser_agent import BrowserActionLog, fetch_page_text
from app.core.permissions import Capability
from app.core.scope import agent_read_scope, build_ctx
from app.models.agent import Agent
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.user import User

router = APIRouter(prefix="/browser-agent", tags=["browser-agent"])


class FetchIn(BaseModel):
    url: str
    render: str | None = None  # None=静态抓取; "js"=Playwright 渲染
    max_chars: int = 8000


async def _my_company_ids(db: AsyncSession, user_id: str) -> list[str]:
    """我可访问的公司：owner 事实源 ∪ active 成员行。"""
    owned = select(Company.id).join(
        User, User.id == Company.owner_id).where(User.id == user_id)
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user_id, CompanyMember.status == "active")
    rows = await db.execute(
        select(Company.id).where(or_(Company.id.in_(owned),
                                     Company.id.in_(member))))
    return [cid for (cid,) in rows.all()]


async def _assert_selectable_company(db: AsyncSession, user: User,
                                     company_id: str,
                                     capability: Capability = Capability.VIEW) -> None:
    """只允许操作服务端推导出的、属于当前用户的公司。越权一律 404。"""
    await assert_company_capability(db, user.id, company_id, capability)


@router.post("/fetch")
async def fetch(
    data: FetchIn,
    company_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 安全抓取网页（自动安全检查 + 审计日志）。"""
    # 出站抓取并落审计日志 = 写操作（OPERATE）
    await _assert_selectable_company(db, user, company_id, Capability.OPERATE)
    result = await fetch_page_text(db, company_id, agent_id, data.url,
                                   max_chars=data.max_chars,
                                   render=data.render)
    if not result["allowed"]:
        raise HTTPException(403, result.get("deny_reason", "blocked"))
    if "error" in result:
        raise HTTPException(502, f"fetch failed: {result['error']}")
    return {"text": result.get("text", ""), "log_id": result["log_id"],
            "title": result.get("title_m"),
            "rendered": result.get("rendered", False),
            "note": result.get("note")}


@router.get("/logs")
async def list_logs(
    company_id: str = Query(...),
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _assert_selectable_company(db, user, company_id)
    ctx = await build_ctx(db, user, company_id)
    conds = [BrowserActionLog.company_id == company_id]
    if not ctx.sees_all_agents:
        # 非 owner/admin 只看到自己可见 Agent 的抓取记录（URL 可能含业务信息）
        conds.append(BrowserActionLog.agent_id.in_(
            select(Agent.id).where(agent_read_scope(ctx))))
    q = await db.execute(select(BrowserActionLog).where(*conds).order_by(
        BrowserActionLog.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    return [{"id": l.id, "agent_id": l.agent_id, "action": l.action_type,
             "url": l.url, "allowed": l.allowed, "deny_reason": l.deny_reason,
             "status_code": l.status_code, "duration_ms": l.duration_ms,
             "created_at": l.created_at.isoformat()} for l in rows]
