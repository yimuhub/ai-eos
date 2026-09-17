"""P2-05: Agent Collaboration Protocol 2.0 API."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.collaboration import (
    AgentCollaboration,
    AgentMessage,
    CollaborationMessageType,
    CollaborationType,
)
from app.models.company import Company
from app.models.agent import Agent
from app.collaboration.routing import send_collaboration_message, route_message
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/collaboration", tags=["collaboration"])


class CollaborationCreate(BaseModel):
    source_agent_id: str
    target_agent_id: str
    collaboration_type: CollaborationType = CollaborationType.COLLABORATOR
    description: str | None = None


class CollaborationResponse(BaseModel):
    id: str
    source_agent_id: str
    target_agent_id: str
    collaboration_type: CollaborationType
    description: str | None
    created_at: str  # 手动序列化

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    sender_agent_id: str
    message_type: CollaborationMessageType
    title: str
    content: str
    context_ref: str | None = None
    context_type: str | None = None
    receiver_ids: list[str] | None = None
    metadata: dict | None = None


class MessageResponse(BaseModel):
    id: str
    sender_agent_id: str
    sender_name: str
    message_type: CollaborationMessageType
    title: str
    content: str
    context_ref: str | None
    context_type: str | None
    receiver_agent_ids: list[str] | None
    read_by: dict | None
    created_at: str

    class Config:
        from_attributes = True


@router.get("/collaborations", response_model=list[CollaborationResponse])
async def list_collaborations(
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出公司的所有 Agent 协作关系。"""
    # 验证公司归属
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    collabs_q = await db.execute(
        select(AgentCollaboration)
        .join(Agent, Agent.id == AgentCollaboration.source_agent_id)
        .where(Agent.company_id == company_id)
    )
    collabs = collabs_q.scalars().all()

    # 手动序列化
    return [
        {
            "id": c.id,
            "source_agent_id": c.source_agent_id,
            "target_agent_id": c.target_agent_id,
            "collaboration_type": c.collaboration_type,
            "description": c.description,
            "created_at": c.created_at.isoformat(),
        }
        for c in collabs
    ]


@router.post("/collaborations", response_model=CollaborationResponse)
async def create_collaboration(
    data: CollaborationCreate,
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建 Agent 协作关系。"""
    # 验证 source 和 target agent 都属于同一公司
    source_agent = await db.get(Agent, data.source_agent_id)
    target_agent = await db.get(Agent, data.target_agent_id)

    if not source_agent or not target_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if source_agent.company_id != company_id or target_agent.company_id != company_id:
        raise HTTPException(status_code=400, detail="Both agents must belong to the same company")

    if source_agent.id == target_agent.id:
        raise HTTPException(status_code=400, detail="Cannot collaborate with yourself")

    # 检查是否已存在相同方向的协作关系
    existing_q = await db.execute(
        select(AgentCollaboration)
        .where(AgentCollaboration.source_agent_id == data.source_agent_id)
        .where(AgentCollaboration.target_agent_id == data.target_agent_id)
    )
    existing = existing_q.scalars().first()

    if existing:
        raise HTTPException(status_code=409, detail="Collaboration relationship already exists")

    collab = AgentCollaboration(
        id=f"collab-{data.source_agent_id[:8]}-{data.target_agent_id[:8]}",
        source_agent_id=data.source_agent_id,
        target_agent_id=data.target_agent_id,
        collaboration_type=data.collaboration_type,
        description=data.description,
    )

    db.add(collab)
    await db.commit()
    await db.refresh(collab)

    # 手动序列化，将 datetime 转为 ISO 字符串
    return {
        "id": collab.id,
        "source_agent_id": collab.source_agent_id,
        "target_agent_id": collab.target_agent_id,
        "collaboration_type": collab.collaboration_type,
        "description": collab.description,
        "created_at": collab.created_at.isoformat(),
    }


@router.delete("/collaborations/{collaboration_id}")
async def delete_collaboration(
    collaboration_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除 Agent 协作关系。"""
    collab = await db.get(AgentCollaboration, collaboration_id)
    if not collab:
        raise HTTPException(status_code=404, detail="Collaboration not found")

    await db.delete(collab)
    await db.commit()

    return {"detail": "Collaboration deleted"}


@router.get("/messages", response_model=list[MessageResponse])
async def list_messages(
    company_id: str = Query(..., description="Company ID"),
    agent_id: str | None = Query(None, description="Filter by sender or receiver agent"),
    message_type: CollaborationMessageType | None = Query(None, description="Filter by message type"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出公司的协作消息。"""
    # 构建查询
    query = select(AgentMessage).join(Agent, Agent.id == AgentMessage.sender_agent_id)

    # 过滤公司
    query = query.where(Agent.company_id == company_id)

    # 过滤消息发送者或接收者
    if agent_id:
        query = query.where(
            (AgentMessage.sender_agent_id == agent_id) |
            (AgentMessage.receiver_agent_ids.contains([agent_id]))
        )

    # 过滤消息类型
    if message_type:
        query = query.where(AgentMessage.message_type == message_type)

    # 按创建时间降序
    query = query.order_by(AgentMessage.created_at.desc())

    # 限制数量
    query = query.limit(limit)

    messages_q = await db.execute(query)
    messages = messages_q.scalars().all()

    # 补充 sender_name
    result = []
    for msg in messages:
        sender = await db.get(Agent, msg.sender_agent_id)
        msg_dict = {
            "id": msg.id,
            "sender_agent_id": msg.sender_agent_id,
            "sender_name": sender.name if sender else "Unknown",
            "message_type": msg.message_type,
            "title": msg.title,
            "content": msg.content,
            "context_ref": msg.context_ref,
            "context_type": msg.context_type,
            "receiver_agent_ids": msg.receiver_agent_ids,
            "read_by": msg.read_by,
            "created_at": msg.created_at.isoformat(),
        }
        result.append(msg_dict)

    return result


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    data: MessageCreate,
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发送一条 Agent 协作消息。"""
    # 验证 sender agent 属于该公司
    sender = await db.get(Agent, data.sender_agent_id)
    if not sender:
        raise HTTPException(status_code=404, detail="Sender agent not found")

    if sender.company_id != company_id:
        raise HTTPException(status_code=400, detail="Sender agent does not belong to the company")

    # 发送消息（自动路由）
    message = await send_collaboration_message(
        db=db,
        sender_id=data.sender_agent_id,
        message_type=data.message_type,
        title=data.title,
        content=data.content,
        company_id=company_id,
        context_ref=data.context_ref,
        context_type=data.context_type,
        receiver_ids=data.receiver_ids,
        metadata=data.metadata,
    )

    # 补充 sender_name
    sender = await db.get(Agent, message.sender_agent_id)
    result = {
        "id": message.id,
        "sender_agent_id": message.sender_agent_id,
        "sender_name": sender.name if sender else "Unknown",
        "message_type": message.message_type,
        "title": message.title,
        "content": message.content,
        "context_ref": message.context_ref,
        "context_type": message.context_type,
        "receiver_agent_ids": message.receiver_agent_ids,
        "read_by": message.read_by,
        "created_at": message.created_at.isoformat(),
    }

    return result


@router.get("/messages/{message_id}", response_model=MessageResponse)
async def get_message(
    message_id: str,
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取单条协作消息详情。"""
    message = await db.get(AgentMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    # 验证消息属于该公司（通过 sender）
    sender = await db.get(Agent, message.sender_agent_id)
    if not sender or sender.company_id != company_id:
        raise HTTPException(status_code=404, detail="Message not found")

    result = {
        "id": message.id,
        "sender_agent_id": message.sender_agent_id,
        "sender_name": sender.name if sender else "Unknown",
        "message_type": message.message_type,
        "title": message.title,
        "content": message.content,
        "context_ref": message.context_ref,
        "context_type": message.context_type,
        "receiver_agent_ids": message.receiver_agent_ids,
        "read_by": message.read_by,
        "created_at": message.created_at.isoformat(),
    }

    return result


@router.post("/messages/{message_id}/read")
async def mark_as_read(
    message_id: str,
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """标记消息为已读。"""
    message = await db.get(AgentMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    sender = await db.get(Agent, message.sender_agent_id)
    if not sender or sender.company_id != company_id:
        raise HTTPException(status_code=404, detail="Message not found")

    # 更新 read_by
    if not message.read_by:
        message.read_by = {}
    message.read_by[sender.id] = True
    await db.commit()

    return {"detail": "Marked as read"}


@router.get("/agents/{agent_id}/graph")
async def get_agent_graph(
    agent_id: str,
    company_id: str = Query(..., description="Company ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取指定 Agent 的协作图谱（连接的 Agent 列表）。"""
    agent = await db.get(Agent, agent_id)
    if not agent or agent.company_id != company_id:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 获取所有与该 agent 相关的协作关系
    src_q = await db.execute(
        select(AgentCollaboration)
        .where(AgentCollaboration.source_agent_id == agent_id)
    )
    collabs_as_source = src_q.scalars().all()

    tgt_q = await db.execute(
        select(AgentCollaboration)
        .where(AgentCollaboration.target_agent_id == agent_id)
    )
    collabs_as_target = tgt_q.scalars().all()

    result = {
        "agent_id": agent_id,
        "agent_name": agent.name,
        "connections": [],
    }

    # 添加 outgoing 连接
    for collab in collabs_as_source:
        target = await db.get(Agent, collab.target_agent_id)
        if target:
            result["connections"].append({
                "type": "outgoing",
                "collaboration_type": collab.collaboration_type.value,
                "target_agent_id": collab.target_agent_id,
                "target_agent_name": target.name,
                "description": collab.description,
            })

    # 添加 incoming 连接
    for collab in collabs_as_target:
        source = await db.get(Agent, collab.source_agent_id)
        if source:
            result["connections"].append({
                "type": "incoming",
                "collaboration_type": collab.collaboration_type.value,
                "source_agent_id": collab.source_agent_id,
                "source_agent_name": source.name,
                "description": collab.description,
            })

    return result


@router.get("/stats/{company_id}")
async def get_collaboration_stats(
    company_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取公司协作统计。"""
    # 统计协作关系数
    collab_count_q = await db.execute(
        select(AgentCollaboration)
        .join(Agent, Agent.id == AgentCollaboration.source_agent_id)
        .where(Agent.company_id == company_id)
    )
    collab_count = len(collab_count_q.scalars().all())

    # 统计消息数
    msg_count_q = await db.execute(
        select(AgentMessage)
        .join(Agent, Agent.id == AgentMessage.sender_agent_id)
        .where(Agent.company_id == company_id)
    )
    msg_count = len(msg_count_q.scalars().all())

    # 按消息类型统计
    type_counts = {}
    for msg_type in CollaborationMessageType:
        type_count_q = await db.execute(
            select(AgentMessage)
            .join(Agent, Agent.id == AgentMessage.sender_agent_id)
            .where(
                Agent.company_id == company_id,
                AgentMessage.message_type == msg_type
            )
        )
        type_counts[msg_type.value] = len(type_count_q.scalars().all())

    return {
        "company_id": company_id,
        "collaboration_count": collab_count,
        "message_count": msg_count,
        "message_type_counts": type_counts,
    }
