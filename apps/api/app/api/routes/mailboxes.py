"""Agent 专属邮箱 API（V5.5 C3）。

- 每 Agent 一个 alias，外部来信 → 收件箱（站内/局域网通道自动注入）
- 老板可代答；SMTP/IMAP 凭据存 env 变量名（Secret 规范，不存明文）
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.permissions import Capability
from app.core.scope import agent_read_scope, build_ctx, ensure_agent_access
from app.models.membership import CompanyMember
from app.models.user import User
from app.models.mailbox import AgentMailbox, MailMessage
from app.models.agent import Agent
from app.models.company import Company

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


async def _agent_owned(db: AsyncSession, user: User, agent_id: str,
                       capability: Capability = Capability.VIEW) -> Agent:
    """Agent 归属 + 行级范围校验（经其所属公司）；越权一律 404。

    邮箱与邮件都没有可直接校验的 company_id（历史数据还写着 "default"），
    统一从关联 Agent 反查公司，再走 agent_write_scope/agent_read_scope。
    """
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    await ensure_agent_access(db, user, agent, capability)
    return agent


async def _mailbox_owned(db: AsyncSession, user: User, mailbox_id: str,
                         capability: Capability = Capability.VIEW) -> AgentMailbox:
    mb = await db.get(AgentMailbox, mailbox_id)
    if not mb:
        raise HTTPException(404, "邮箱不存在")
    await _agent_owned(db, user, mb.agent_id, capability)
    return mb


async def _message_owned(db: AsyncSession, user: User, msg_id: str,
                         capability: Capability = Capability.VIEW) -> MailMessage:
    m = await db.get(MailMessage, msg_id)
    if not m:
        raise HTTPException(404, "邮件不存在")
    await _mailbox_owned(db, user, m.mailbox_id, capability)
    return m


async def _visible_agent_ids(db: AsyncSession, user: User):
    """调用者可读的 Agent id 子查询（邮箱/邮件的可访问范围）。

    与 agent_read_scope 同构：先取可访问公司，再逐公司套行级可见条件。
    不再手写 `companies.owner_id` 与用户 id 的等值比较——P5 之后成员身份在成员表里。
    """
    if getattr(user, "is_superadmin", False):
        cids = [cid for (cid,) in (await db.execute(select(Company.id))).all()]
    else:
        owned = select(Company.id).join(
            User, User.id == Company.owner_id).where(User.id == user.id)
        member = select(CompanyMember.company_id).where(
            CompanyMember.user_id == user.id,
            CompanyMember.status == "active")
        cids = (await db.execute(select(Company.id).where(
            or_(Company.id.in_(owned), Company.id.in_(member))))).scalars().all()

    scopes = []
    for cid in cids:
        try:
            ctx = await build_ctx(db, user, cid)
        except HTTPException:
            continue
        scopes.append(agent_read_scope(ctx))
    if not scopes:
        # 无任何公司可访问 → 空集（不能退化成"全表"）
        return select(Agent.id).where(Agent.id.is_(None))
    return select(Agent.id).where(or_(*scopes))


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
    agent = await _agent_owned(db, user, data.agent_id, Capability.OPERATE)
    exists = (await db.execute(
        select(AgentMailbox).where(AgentMailbox.alias == data.alias)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "别名已存在")
    mb = AgentMailbox(
        id=str(uuid.uuid4()), agent_id=data.agent_id,
        company_id=agent.company_id,
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
    q = select(AgentMailbox).where(AgentMailbox.agent_id.in_(
        await _visible_agent_ids(db, user)))
    if agent_id:
        await _agent_owned(db, user, agent_id)
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
    mb = await _mailbox_owned(db, user, data.mailbox_id, Capability.OPERATE)
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
    owned_mailboxes = select(AgentMailbox.id).where(
        AgentMailbox.agent_id.in_(await _visible_agent_ids(db, user)))
    q = (select(MailMessage).where(MailMessage.mailbox_id.in_(owned_mailboxes))
         .order_by(MailMessage.created_at.desc()).limit(limit))
    if mailbox_id:
        await _mailbox_owned(db, user, mailbox_id)
        q = q.where(MailMessage.mailbox_id == mailbox_id)
    elif agent_id:
        await _agent_owned(db, user, agent_id)
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
    m = await _message_owned(db, user, msg_id, Capability.OPERATE)
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
    m = await _message_owned(db, user, msg_id, Capability.OPERATE)
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
