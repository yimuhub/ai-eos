"""CEO 常驻助手接口：入口卡片、线程列表、线程消息与手动删除。

对话原文落在 `agent_chat_messages`，线程只负责归属与滚动摘要；
聊天本身复用 `/agents/{aid}/chat/stream`（payload 里带 `thread_id`）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.ceo_companion import (companion_payload, create_thread,
                               delete_thread, empty_companion_payload,
                               ensure_default_thread, list_threads,
                               thread_payload)
from app.ceo_engine import find_coordinator
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.db.session import get_db
from app.models.agent import Agent
from app.models.agent_chat import AgentChatMessage
from app.models.ceo_thread import CeoThread
from app.schemas.responses_ceo import (CeoCompanionOut, CeoThreadDeleteOut,
                                       CeoThreadListOut, CeoThreadMessagesOut,
                                       CeoThreadOut)

router = APIRouter(prefix="/ceo", tags=["ceo-companion"])
logger = get_logger(__name__)


class CreateThreadIn(BaseModel):
    company_id: str
    title: str | None = None


async def _thread_in_company(db: AsyncSession, thread_id: str,
                             company_id: str) -> CeoThread:
    thread = await db.get(CeoThread, thread_id)
    if thread is None or str(thread.company_id) != company_id:
        raise HTTPException(404, "对话不存在")
    agent = await db.get(Agent, str(thread.agent_id))
    if agent is None or str(agent.company_id) != company_id:
        raise HTTPException(404, "对话不存在")
    return thread


@router.get("/companion", response_model=CeoCompanionOut)
async def get_companion(company_id: str,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, company_id,
                                         Capability.VIEW)
    agent = await find_coordinator(db, company_id)
    if agent is None:
        return empty_companion_payload(company_id)
    payload = await companion_payload(db, company_id, agent)
    await db.commit()
    return payload


@router.get("/threads", response_model=CeoThreadListOut)
async def get_threads(company_id: str,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, company_id,
                                         Capability.VIEW)
    agent = await find_coordinator(db, company_id)
    if agent is None:
        return {"has_ceo": False, "company_id": company_id,
                "ceo_agent_id": None, "threads": []}
    await ensure_default_thread(db, agent)
    await db.commit()
    rows = await list_threads(db, str(agent.id))
    return {"has_ceo": True, "company_id": company_id,
            "ceo_agent_id": str(agent.id),
            "threads": [thread_payload(r) for r in rows]}


@router.post("/threads", response_model=CeoThreadOut, status_code=201)
async def post_thread(data: CreateThreadIn,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, data.company_id,
                                         Capability.OPERATE)
    agent = await find_coordinator(db, data.company_id)
    if agent is None:
        raise HTTPException(409, "本公司还没有指定 CEO")
    thread = await create_thread(db, agent, data.title)
    await db.commit()
    return thread_payload(thread)


@router.get("/threads/{thread_id}/messages",
            response_model=CeoThreadMessagesOut)
async def get_thread_messages(thread_id: str, company_id: str,
                              limit: int = Query(200, ge=1, le=500),
                              db: AsyncSession = Depends(get_db),
                              user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, company_id,
                                         Capability.VIEW)
    thread = await _thread_in_company(db, thread_id, company_id)
    rows = (await db.execute(
        select(AgentChatMessage)
        .where(AgentChatMessage.thread_id == str(thread.id))
        .order_by(AgentChatMessage.created_at.desc()).limit(limit)
    )).scalars().all()
    return {
        "thread_id": str(thread.id),
        "title": thread.title,
        "summary": thread.summary,
        "messages": [
            {"id": r.id, "role": r.role, "content": r.content,
             "meta": r.meta, "thread_id": r.thread_id,
             "created_at": str(r.created_at)}
            for r in reversed(rows)
        ],
    }


@router.delete("/threads/{thread_id}", response_model=CeoThreadDeleteOut)
async def remove_thread(thread_id: str, company_id: str,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(deps.get_current_user)):
    await deps.assert_company_capability(db, user.id, company_id,
                                         Capability.MANAGE)
    thread = await _thread_in_company(db, thread_id, company_id)
    await delete_thread(db, thread)
    await db.commit()
    return {"ok": True, "thread_id": thread_id}
