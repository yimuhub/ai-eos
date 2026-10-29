"""Browser Agent API (P2-13).

租户边界：可访问公司集合由服务端从 `Company.owner_id == user.id` 推导，
客户端传入的 company_id 只用于“选定其中一个”，不再被直接当作过滤条件。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.browser_agent import BrowserActionLog, fetch_page_text
from app.models.company import Company
from app.models.user import User

router = APIRouter(prefix="/browser-agent", tags=["browser-agent"])


class FetchIn(BaseModel):
    url: str
    render: str | None = None  # None=静态抓取; "js"=Playwright 渲染
    max_chars: int = 8000


async def _my_company_ids(db: AsyncSession, user_id: str) -> list[str]:
    rows = await db.execute(select(Company.id).where(
        Company.owner_id == user_id))
    return [cid for (cid,) in rows.all()]


async def _assert_selectable_company(db: AsyncSession, user: User,
                                     company_id: str) -> None:
    """只允许操作服务端推导出的、属于当前用户的公司。越权一律 404。"""
    allowed = await _my_company_ids(db, user.id)
    if company_id not in allowed:
        raise HTTPException(404, "Company not found")


@router.post("/fetch")
async def fetch(
    data: FetchIn,
    company_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 安全抓取网页（自动安全检查 + 审计日志）。"""
    await _assert_selectable_company(db, user, company_id)
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
    # 即使上面的校验被误删，这里也只查服务端推导出的公司集合
    allowed = await _my_company_ids(db, user.id)
    q = await db.execute(select(BrowserActionLog).where(
        BrowserActionLog.company_id.in_(allowed)).order_by(
        BrowserActionLog.created_at.desc()).limit(limit))
    rows = q.scalars().all()
    return [{"id": l.id, "agent_id": l.agent_id, "action": l.action_type,
             "url": l.url, "allowed": l.allowed, "deny_reason": l.deny_reason,
             "status_code": l.status_code, "duration_ms": l.duration_ms,
             "created_at": l.created_at.isoformat()} for l in rows]
