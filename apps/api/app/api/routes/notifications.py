"""站内通知 API（C4）。

租户边界与其它端点保持一致：
* 列表/未读数/全部已读都必须带 company_id，并要求调用者在该公司有 VIEW；
* 单条已读只认 `user_id = 当前用户`，他人通知与"不属于该公司的通知"
  一律 404（不泄露通知是否存在）。
"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.notification import Notification
from app.models.push_subscription import PushSubscription
from app.models.user import User
from app.services import web_push

router = APIRouter(prefix="/notifications", tags=["notifications"])


class PushKeysIn(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=128)


class PushSubscribeIn(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)
    endpoint: str = Field(min_length=1, max_length=2000)
    keys: PushKeysIn


class PushUnsubscribeIn(BaseModel):
    company_id: str | None = Field(default=None, max_length=36)
    endpoint: str | None = Field(default=None, max_length=2000)


def _out(n: Notification) -> dict:
    return {
        "id": n.id,
        "company_id": n.company_id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "is_read": bool(n.is_read),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
async def list_notifications(
    company_id: str = Query(...),
    unread_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前用户在某公司的通知，按时间倒序；limit 上限 50。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    q = select(Notification).where(
        Notification.user_id == user.id,
        Notification.company_id == company_id)
    if unread_only:
        q = q.where(Notification.is_read.is_(False))
    rows = (await db.execute(
        q.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit))).scalars().all()
    return [_out(n) for n in rows]


@router.get("/unread-count")
async def unread_count(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """铃铛徽标：只统计当前用户、当前公司的未读。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    total = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id,
            Notification.company_id == company_id,
            Notification.is_read.is_(False)))).scalar()
    return {"count": int(total or 0)}


@router.get("/push/public-key")
async def push_public_key():
    """前端订阅所需的 VAPID 公钥；未配置时 configured=false、公钥为 null。"""
    return web_push.push_status()


@router.post("/push/subscribe")
async def push_subscribe(
    payload: PushSubscribeIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记当前浏览器的推送订阅；同一 endpoint 幂等 upsert 到当前用户。"""
    await assert_company_capability(
        db, user.id, payload.company_id, Capability.VIEW)
    try:
        web_push.validate_endpoint(payload.endpoint)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid push endpoint: {exc}") from exc

    user_agent = (request.headers.get("user-agent") or "")[:500] or None
    row = (await db.execute(select(PushSubscription).where(
        PushSubscription.endpoint == payload.endpoint))).scalars().first()
    if row is None:
        row = PushSubscription(
            company_id=payload.company_id,
            user_id=user.id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=user_agent,
        )
        db.add(row)
    else:
        # 浏览器会轮换 p256dh/auth；同一 endpoint 换账号登录时也归到当前用户。
        row.company_id = payload.company_id
        row.user_id = user.id
        row.p256dh = payload.keys.p256dh
        row.auth = payload.keys.auth
        row.user_agent = user_agent
    await db.commit()
    return {"subscribed": True, "id": row.id}


@router.delete("/push/unsubscribe")
async def push_unsubscribe(
    endpoint: str | None = Query(default=None, max_length=2000),
    company_id: str | None = Query(default=None, max_length=36),
    payload: PushUnsubscribeIn | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """退订：只删当前用户自己的订阅；不存在也 200（幂等）。

    endpoint/company_id 可走 query（前端 api.del）或 JSON body，二选一即可。
    """
    endpoint = (payload.endpoint if payload else None) or endpoint
    company_id = (payload.company_id if payload else None) or company_id
    if not endpoint:
        raise HTTPException(400, "endpoint is required")
    if company_id:
        await assert_company_capability(db, user.id, company_id, Capability.VIEW)
        res = await db.execute(
            delete(PushSubscription).where(
                PushSubscription.user_id == user.id,
                PushSubscription.company_id == company_id,
                PushSubscription.endpoint == endpoint)
            .execution_options(synchronize_session=False))
    else:
        # 修复：docstring 说"二选一"，实现却把缺 company_id 一律 400。
        # 现在按 endpoint 反查该用户自己的订阅，逐条校验公司归属后删除
        # 可见的那些（不可见的等价于"不存在"，不泄露）。
        owned = (await db.execute(select(PushSubscription).where(
            PushSubscription.user_id == user.id,
            PushSubscription.endpoint == endpoint))).scalars().all()
        deleted = 0
        for sub in owned:
            try:
                await assert_company_capability(db, user.id, sub.company_id,
                                                Capability.VIEW)
            except HTTPException:
                continue
            await db.delete(sub)
            deleted += 1
        await db.commit()
        return {"deleted": deleted}
    await db.commit()
    return {"deleted": int(res.rowcount or 0)}


@router.post("/push/test")
async def push_test(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """给当前用户发一条测试推送；未配置/依赖缺失统一 503。"""
    status = web_push.push_status()
    if not status["available"]:
        reason = status.get("reason")
        detail = ("推送依赖未安装（pywebpush）" if reason == "dependency_missing"
                  else "推送未配置（缺少 VAPID 密钥）")
        return JSONResponse(
            status_code=503,
            content={"detail": detail, "code": "push_not_configured"},
        )

    result = await web_push.send_to_user(
        db, user.id, "AI-EOS 测试通知",
        "如果你看到这条消息，说明移动端推送已开启。", url="/m")
    return {"sent": result["sent"], "failed": result["failed"]}


@router.post("/read-all")
async def mark_all_read(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把当前用户在该公司的未读全部置已读；别人的行不受影响。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    res = await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id,
               Notification.company_id == company_id,
               Notification.is_read.is_(False))
        .values(is_read=True)
        .execution_options(synchronize_session=False))
    await db.commit()
    return {"updated": int(res.rowcount or 0)}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单条已读。他人通知 → 404；通知所属公司无权访问 → 404。"""
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    await assert_company_capability(db, user.id, n.company_id, Capability.VIEW)
    if not n.is_read:
        n.is_read = True
        await db.commit()
    return _out(n)
