"""会话已读同步（C3）。

* `GET  /thread-reads?company_id=` —— 拉取当前用户在该公司的全部已读位置；
* `POST /thread-reads`            —— 批量合并（只增不减），供切设备/多标签收敛。

只同步"已读时间戳"，不同步置顶/归档：后者是每台设备的摆放偏好，
强行同步反而会让用户在手机上整理过的左栏被桌面覆盖。

合并规则与前端 `markRead` 保持一致：同一会话取较大的时间戳。客户端时钟
可能超前，服务端把上限钳到「当前时间 + 5 分钟」，避免一个坏时钟把未来
所有消息长期标成已读。时间戳用毫秒整数而不是 DATETIME：前端 localStorage
本来就是毫秒，少一层时区换算就少一类"跨时区差 8 小时"的 bug。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.thread_read import ThreadRead
from app.models.user import User

router = APIRouter(prefix="/thread-reads", tags=["thread-reads"])

MAX_BATCH = 200
FUTURE_SKEW_MS = 5 * 60 * 1000


class ThreadReadIn(BaseModel):
    thread_key: str = Field(min_length=1, max_length=160)
    read_at: int = Field(ge=0)


class ThreadReadsIn(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)
    reads: list[ThreadReadIn] = Field(min_length=1, max_length=MAX_BATCH)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _out(row: ThreadRead) -> dict:
    return {"thread_key": row.thread_key, "read_at": int(row.read_at_ms)}


@router.get("")
async def list_thread_reads(
    company_id: str = Query(..., min_length=1, max_length=36),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前用户在某公司的已读位置；别的公司的行不会出现在这里。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = (await db.execute(select(ThreadRead).where(
        ThreadRead.user_id == user.id,
        ThreadRead.company_id == company_id))).scalars().all()
    return {"reads": [_out(r) for r in rows], "server_time": _now_ms()}


@router.post("")
async def merge_thread_reads(
    payload: ThreadReadsIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量合并已读位置，只增不减；返回合并后的全量快照。"""
    await assert_company_capability(
        db, user.id, payload.company_id, Capability.VIEW)

    # 同一批里重复 key 时先取 max，避免后写覆盖前写
    incoming: dict[str, int] = {}
    ceiling = _now_ms() + FUTURE_SKEW_MS
    for item in payload.reads:
        key = item.thread_key.strip()
        if not key:
            raise HTTPException(400, "thread_key 不能为空")
        ts = min(int(item.read_at), ceiling)
        if ts > incoming.get(key, -1):
            incoming[key] = ts

    existing = {
        row.thread_key: row
        for row in (await db.execute(select(ThreadRead).where(
            ThreadRead.user_id == user.id,
            ThreadRead.company_id == payload.company_id,
            ThreadRead.thread_key.in_(list(incoming))))).scalars().all()
    }

    changed = 0
    for key, ts in incoming.items():
        row = existing.get(key)
        if row is None:
            db.add(ThreadRead(
                user_id=user.id, company_id=payload.company_id,
                thread_key=key, read_at_ms=ts))
            changed += 1
        elif ts > int(row.read_at_ms or 0):
            row.read_at_ms = ts
            changed += 1

    try:
        await db.commit()
    except IntegrityError:
        # 同一用户两台设备同时首写同一会话：唯一约束兜底，重试时只做更新
        await db.rollback()
        changed = await _merge_existing(db, user.id, payload.company_id,
                                        incoming)

    rows = (await db.execute(select(ThreadRead).where(
        ThreadRead.user_id == user.id,
        ThreadRead.company_id == payload.company_id))).scalars().all()
    return {
        "reads": [_out(r) for r in rows],
        "updated": changed,
        "server_time": _now_ms(),
    }


async def _merge_existing(db: AsyncSession, user_id: str, company_id: str,
                          incoming: dict[str, int]) -> int:
    rows = (await db.execute(select(ThreadRead).where(
        ThreadRead.user_id == user_id,
        ThreadRead.company_id == company_id,
        ThreadRead.thread_key.in_(list(incoming))))).scalars().all()
    changed = 0
    for row in rows:
        ts = incoming.get(row.thread_key)
        if ts is not None and ts > int(row.read_at_ms or 0):
            row.read_at_ms = ts
            changed += 1
    await db.commit()
    return changed
