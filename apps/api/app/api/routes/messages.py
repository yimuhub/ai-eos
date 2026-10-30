"""Agent communication: conversations + messages (user can join too)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.core.scope import agent_read_scope, build_ctx
from app.db.session import get_db
from app.models.agent import Agent
from app.models.message import Conversation, Message, MessageType
from app.models.task import Task, TaskPriority, TaskStatus
from app.services.event_system import log_event

router = APIRouter(tags=["communication"])


class ConversationIn(BaseModel):
    company_id: str
    title: str | None = None
    participants: list[str] = []  # agent ids
    task_id: str | None = None


class MessageIn(BaseModel):
    sender_type: str = "user"  # user|agent|system
    sender_id: str | None = None
    receiver_id: str | None = None
    message_type: str = "MESSAGE"
    content: str


async def _visible_agent_ids(db: AsyncSession, ctx) -> set[str]:
    rows = (await db.execute(
        select(Agent.id).where(agent_read_scope(ctx)))).scalars().all()
    return {str(x) for x in rows}


async def _conv_owned(db: AsyncSession, user, conv_id: str,
                      capability: Capability = Capability.VIEW) -> Conversation:
    cv = await db.get(Conversation, conv_id)
    if not cv:
        raise HTTPException(404, "Conversation not found")
    await assert_company_capability(db, user.id, cv.company_id, capability)
    ctx = await build_ctx(db, user, cv.company_id)
    if not ctx.sees_all_agents:
        # 会话没有 created_by 字段：用"参与者里有我可见的 Agent"作为行级口径
        visible = await _visible_agent_ids(db, ctx)
        if not visible.intersection({str(p) for p in (cv.participants or [])}):
            raise HTTPException(404, "Conversation not found")
    return cv


@router.get("/conversations")
async def list_conversations(company_id: str,
                             db: AsyncSession = Depends(get_db),
                             user=Depends(get_current_user)):
    ctx = await build_ctx(db, user, company_id)
    q = await db.execute(select(Conversation).where(
        Conversation.company_id == company_id))
    rows = list(q.scalars())
    if not ctx.sees_all_agents:
        visible = await _visible_agent_ids(db, ctx)
        rows = [cv for cv in rows
                if visible.intersection({str(p) for p in (cv.participants or [])})]
    return [{c.key: getattr(cv, c.key) for c in cv.__table__.columns} for cv in rows]


@router.post("/conversations", status_code=201)
async def create_conversation(data: ConversationIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    # 建会话 = 日常写操作（OPERATE）
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.OPERATE)
    cv = Conversation(**data.model_dump())
    db.add(cv)
    await db.commit()
    await db.refresh(cv)
    return {col.key: getattr(cv, col.key) for col in cv.__table__.columns}


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    cv = await _conv_owned(db, user, cid)
    d = {c.key: getattr(cv, c.key) for c in cv.__table__.columns}
    mq = await db.execute(select(Message).where(Message.conversation_id == cid)
                          .order_by(Message.created_at))
    d["messages"] = [
        {"id": m.id, "sender_type": m.sender_type, "sender_id": m.sender_id,
         "receiver_id": m.receiver_id, "message_type": m.message_type.value,
         "content": m.content, "created_at": str(m.created_at)}
        for m in mq.scalars()]
    return d


@router.post("/conversations/{cid}/messages", status_code=201)
async def post_message(cid: str, data: MessageIn,
                       background: BackgroundTasks,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    await _conv_owned(db, user.id, cid)
    m = Message(conversation_id=cid, sender_type=data.sender_type,
                sender_id=data.sender_id, receiver_id=data.receiver_id,
                message_type=MessageType(data.message_type), content=data.content)
    db.add(m)
    await log_event(db, "message.sent", "conversation", cid, data.sender_type,
                    data.sender_id, {"type": data.message_type})
    await db.commit()
    await db.refresh(m)

    # 用户发言后，触发会话中的 Agent 自动回复（P1-09 协同闭环）
    if data.sender_type == "user":
        import app.background_jobs as background_jobs
        background_jobs.submit_async(lambda session: _agents_reply(
            session, cid, speaker="老板"))

    return {"id": m.id, "content": m.content, "sender_type": m.sender_type,
            "created_at": str(m.created_at)}


async def _agents_reply(session: AsyncSession, conv_id: str, speaker: str = "用户"):
    """会话中的每个 Agent 依次针对最新上下文回复一条消息。"""
    from app.agent_runtime import build_context
    from app.model_gateway import ModelGateway
    from app.models.agent import Agent

    cv = await session.get(Conversation, conv_id)
    if not cv:
        return
    mq = await session.execute(
        select(Message).where(Message.conversation_id == conv_id)
        .order_by(Message.created_at))
    history = [
        {"role": "assistant" if m.sender_type == "agent" else "user",
         "content": m.content}
        for m in mq.scalars()
    ]
    if not history:
        return

    participant_ids = (cv.participants or [])[:3]  # 最多 3 个 Agent 回复，控制成本
    for agent_id in participant_ids:
        agent = await session.get(Agent, agent_id)
        if not agent:
            continue
        try:
            messages, model_ids = await build_context(
                session, agent,
                Task(id="conv", company_id=cv.company_id,
                     title=f"协作会话：{cv.title or '讨论'}",
                     description="", status=TaskStatus.TODO,
                     priority=TaskPriority.NORMAL, created_by="system"))
            # 用会话历史替换默认 user 消息
            messages = messages[:1] + history + [{
                "role": "system",
                "content": f"你正在和老板及其他同事开会讨论。请以 {agent.name} 的身份"
                           f"简短发言（100字以内），回应老板的最新发言，可提出不同意见。",
            }]
            gateway = ModelGateway(session)
            result = await gateway.chat_with_agent_models(model_ids, messages,
                                                          config={"max_tokens": 500})
            reply = Message(conversation_id=conv_id, sender_type="agent",
                            sender_id=agent.id, message_type=MessageType.MESSAGE,
                            content=result["content"])
            session.add(reply)
            await log_event(session, "message.sent", "conversation", conv_id,
                            "agent", agent.id, {"type": "MESSAGE"})
            await session.commit()
        except Exception:
            await session.rollback()
