"""持久化后台队列的可观测与人工干预入口。

只暴露"看得见、能取消"两件事：
* 列表默认**不回传 payload**——payload 里可能夹带密钥或业务正文，
  租户成员只该知道"有什么在排队"，不该看到别人任务的入参；
* 取消只允许 QUEUED：RUNNING 由执行方自己收尾（强杀会留下半截状态），
  已完成/已失败/已取消的条目重放没有意义，一律 409。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability, ensure_capability
from app.db.session import get_db
from app.models.queue_item import QueueItem, QueueItemStatus
from app.schemas.responses import QueueCancelOut, QueueItemOut, QueueStatsOut
from app.services import job_queue

router = APIRouter(prefix="/queue", tags=["queue"])

LIST_LIMIT = 200


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _item_out(item: QueueItem) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": _status_value(item.status),
        "attempts": item.attempts,
        "max_attempts": item.max_attempts,
        "available_at": item.available_at,
        "last_error": item.last_error,
        "created_at": item.created_at,
    }


@router.get("", response_model=list[QueueItemOut])
async def list_queue(company_id: str = Query(...),
                     limit: int = Query(LIST_LIMIT, ge=1, le=LIST_LIMIT),
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = (await db.execute(
        select(QueueItem)
        .where(QueueItem.company_id == company_id)
        .order_by(QueueItem.created_at.desc())
        .limit(limit))).scalars().all()
    return [_item_out(i) for i in rows]


@router.get("/stats", response_model=QueueStatsOut)
async def queue_stats(company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    data = await job_queue.stats(db, company_id)
    return {"company_id": company_id, **data}


@router.post("/{item_id}/cancel", response_model=QueueCancelOut)
async def cancel_queue_item(item_id: str, db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    item = await db.get(QueueItem, item_id)
    if item is None or item.company_id is None:
        raise HTTPException(404, "Queue item not found")
    # MANAGE：取消别人排队中的执行是"可逆的破坏"，普通成员不行
    await ensure_capability(db, user.id, item.company_id, Capability.MANAGE)
    if item.status == QueueItemStatus.RUNNING:
        raise HTTPException(409, "该条目已在执行中，无法取消")
    if item.status != QueueItemStatus.QUEUED:
        raise HTTPException(409, f"该条目已结束（{_status_value(item.status)}），无法取消")
    if not await job_queue.cancel(db, item.id):
        # 走到这里说明刚好被 worker 领走：与"已在执行"同语义
        raise HTTPException(409, "该条目已被领取执行，无法取消")
    await db.refresh(item)
    return {"detail": "cancelled", "id": item.id, "status": _status_value(item.status)}
