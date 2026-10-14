"""Memory API (P2-07)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.memory_engine import cleanup_expired, forget, recall, remember
from app.models.memory import MemoryType
from app.models.user import User

router = APIRouter(prefix="/memory", tags=["memory"])


class RememberIn(BaseModel):
    agent_id: str
    memory_type: MemoryType
    content: str
    importance: float | None = None
    context_ref: str | None = None


@router.post("/remember")
async def remember_ep(
    data: RememberIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mem = await remember(db, data.agent_id, company_id, data.memory_type,
                         data.content, data.importance, data.context_ref)
    if mem is None:
        return {"stored": False, "reason": "importance too low"}
    return {"stored": True, "id": mem.id,
            "expires_at": mem.expires_at.isoformat() if mem.expires_at else None}


@router.get("/recall")
async def recall_ep(
    company_id: str = Query(...),
    agent_id: str = Query(...),
    q: str | None = Query(None),
    memory_type: MemoryType | None = Query(None),
    top_k: int = Query(8, ge=1, le=50),
    min_importance: float = Query(0.0, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    memories = await recall(db, agent_id, q, memory_type, top_k, min_importance)
    return {"memories": memories, "count": len(memories)}


@router.delete("/{agent_id}/{memory_id}")
async def forget_ep(
    agent_id: str,
    memory_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 归属校验：原实现直接按 id 遗忘，任何登录用户都能删掉别人员工的记忆
    from app.api.deps import assert_agent_owned
    await assert_agent_owned(db, user.id, agent_id)
    ok = await forget(db, agent_id, memory_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"detail": "forgotten"}


@router.post("/cleanup")
async def cleanup_ep(
    company_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    removed = await cleanup_expired(db, company_id)
    return {"removed": removed}
