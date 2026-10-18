"""Agent 专属邮箱 API（V5.5 C3）。

- 每 Agent 一个 alias，外部来信 → 收件箱（站内/局域网通道自动注入）
- 老板可代答；SMTP/IMAP 凭据存 env 变量名（Secret 规范，不存明文）
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.models.mailbox import AgentMailbox, MailMessage
from app.models.agent import Agent

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


async def _agent_owned(db: AsyncSession, agent_id: str) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    return agent


class MailboxIn(BaseModel):
    agent_id: str
    alias: str
    smtp_env: str | None = None
    imap_env: str | None = None
    auto_reply: bool = True


class MsgIn(BaseModel):
    mailbox_id: str
    subject: str
    body: str
    from_addr: str = "boss@aieos.local"
    to_addr: str = ""


@router.post("")
async def create_mailbox(
    data: MailboxIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _agent_owned(db, data.agent_id)
    exists = (await db.execute(
        select(AgentMailbox).where(AgentMailbox.alias == data.alias)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "别名已存在")
    mb = AgentMailbox(
        id=str(uuid.uuid4()), agent_id=data.agent_id,
        company_id="default",
        alias=data.alias,
        smtp_env=data.smtp_env, imap_env=data.imap_env,
        auto_reply=data.auto_reply)
    db.add(mb)
    await db.commit()
    return {"id": mb.id, "alias": mb.alias, "agent_id": mb.agent_id,
            "auto_reply": mb.auto_reply}


@router.get("")
async def list_mailboxes(
    agent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(AgentMailbox)
    if agent_id:
        q = q.where(AgentMailbox.agent_id == agent_id)
    rows = (await db.execute(q)).scalars().all()
    return [
        {"id": m.id, "agent_id": m.agent_id, "alias": m.alias,
         "smtp_env": m.smtp_env, "imap_env": m.imap_env,
         "auto_reply": m.auto_reply}
        for m in rows
    ]


@router.post("/messages")
async def receive_message(
    data: MsgIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """外部来信（站内/局域网入口或 IMAP 同步器调用）→ 收件箱。"""
    mb = (await db.execute(
        select(AgentMailbox).where(AgentMailbox.id == data.mailbox_id)
    )).scalar_one_or_none()
    if not mb:
        raise HTTPException(404, "邮箱不存在")
    msg = MailMessage(
        id=str(uuid.uuid4()), mailbox_id=mb.id, direction="in",
        from_addr=data.from_addr, to_addr=mb.alias,
        subject=data.subject, body=data.body,
        status="unread", channel="inbox",
        raw={"agent_id": mb.agent_id})
    db.add(msg)
    await db.commit()
    return {"id": msg.id, "status": msg.status, "agent_id": mb.agent_id}


@router.get("/messages")
async def list_messages(
    mailbox_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(MailMessage).order_by(MailMessage.created_at.desc()).limit(limit)
    if mailbox_id:
        q = q.where(MailMessage.mailbox_id == mailbox_id)
    elif agent_id:
        sub = (await db.execute(
            select(AgentMailbox.id).where(AgentMailbox.agent_id == agent_id)
        )).scalars().all()
        q = q.where(MailMessage.mailbox_id.in_(list(sub)))
    rows = (await db.execute(q)).scalars().all()
    return [
        {"id": m.id, "mailbox_id": m.mailbox_id, "direction": m.direction,
         "from": m.from_addr, "to": m.to_addr, "subject": m.subject,
         "body": m.body, "status": m.status, "channel": m.channel,
         "created_at": str(m.created_at)}
        for m in rows
    ]


@router.post("/messages/{msg_id}/read")
async def mark_read(
    msg_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = (await db.execute(
        select(MailMessage).where(MailMessage.id == msg_id)
    )).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "邮件不存在")
    m.status = "read" if m.status == "unread" else m.status
    await db.commit()
    return {"id": m.id, "status": m.status}


@router.post("/messages/{msg_id}/reply")
async def reply(
    msg_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """老板代答 / Agent 回复 → 记 out 件并把原信标 replied。"""
    m = (await db.execute(
        select(MailMessage).where(MailMessage.id == msg_id)
    )).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "邮件不存在")
    out = MailMessage(
        id=str(uuid.uuid4()), mailbox_id=m.mailbox_id, direction="out",
        from_addr="boss@aieos.local", to_addr=m.from_addr or m.to_addr,
        subject=f"Re: {m.subject}", body=data.get("body", ""),
        status="sent", channel="inbox",
        raw={"reply_to": m.id})
    m.status = "replied"
    db.add(out)
    await db.commit()
    return {"reply_id": out.id, "original": m.id}
