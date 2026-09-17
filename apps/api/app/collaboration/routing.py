"""Collaboration routing engine (P2-05).

Routes AgentMessage to appropriate recipients based on:
1. Explicit receiver_agent_ids in the message
2. Collaboration graph edges (who is connected to the sender)
3. Message type heuristics (e.g., ESCALATE goes to managers)
"""
import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentModelConfig
from app.models.collaboration import (
    AgentCollaboration,
    AgentMessage,
    CollaborationType,
    CollaborationMessageType,
)
from app.models.company import Company, Department


async def route_message(
    db: AsyncSession,
    message: AgentMessage,
    sender: Agent,
    company: Company,
) -> list[str]:
    """根据协作关系和消息类型确定接收者列表。"""
    receiver_ids: list[str] = []

    # 1. 如果消息已有显式指定接收者，优先使用
    if message.receiver_agent_ids:
        receiver_ids.extend(message.receiver_agent_ids)

    # 2. 否则根据消息类型和协作关系自动路由
    if not receiver_ids:
        receiver_ids = await _auto_route(db, message, sender, company)

    # 3. 去重并过滤无效的 agent_id
    seen: set[str] = set()
    unique: list[str] = []
    for rid in receiver_ids:
        if rid and rid not in seen:
            seen.add(rid)
            unique.append(rid)
    message.receiver_agent_ids = unique
    return unique


async def _auto_route(
    db: AsyncSession,
    message: AgentMessage,
    sender: Agent,
    company: Company,
) -> list[str]:
    """根据消息类型和协作图谱自动路由。"""
    from app.models.agent import Agent as AgentModel  # avoid circular import

    agents_q = await db.execute(select(AgentModel).where(AgentModel.company_id == company.id))
    agents = agents_q.scalars().all()
    agent_ids = {a.id for a in agents}

    if message.message_type == CollaborationMessageType.ESCALATE:
        # 升级请求发给上级管理者
        return await _route_to_managers(db, sender.id, agent_ids)
    elif message.message_type == CollaborationMessageType.TASK_ASSIGNED:
        # 任务指派发给指定的 collaborator
        return await _route_to_collaborators(db, sender.id, agent_ids)
    elif message.message_type in (CollaborationMessageType.DISCUSSION,
                                  CollaborationMessageType.DECISION,
                                  CollaborationMessageType.VOTE,
                                  CollaborationMessageType.CONFLICT):
        # 讨论/决策/投票类消息广播给所有协作者
        return await _route_to_all_collaborators(db, sender.id, agent_ids)
    elif message.message_type == CollaborationMessageType.QUESTION:
        # 提问发给相关协作者或所有人（取决于 context）
        if message.context_ref:
            # 如果有上下文关联，发给该上下文相关的协作者
            return await _route_by_context(db, message.context_ref, sender.id, agent_ids)
        return await _route_to_all_collaborators(db, sender.id, agent_ids)
    elif message.message_type == CollaborationMessageType.ANSWER:
        # 回答只发给提问的 sender
        return [sender.id]
    elif message.message_type in (CollaborationMessageType.PROGRESS,
                                  CollaborationMessageType.BLOCKED,
                                  CollaborationMessageType.TASK_COMPLETED):
        # 进度/阻塞/完成发给管理者
        return await _route_to_managers(db, sender.id, agent_ids)
    elif message.message_type == CollaborationMessageType.DONE:
        # 完成通知发给所有相关协作者
        return await _route_to_all_collaborators(db, sender.id, agent_ids)
    else:
        # 默认广播给所有协作者
        return await _route_to_all_collaborators(db, sender.id, agent_ids)


async def _route_to_managers(
    db: AsyncSession, sender_id: str, agent_ids: set[str]
) -> list[str]:
    """路由给 sender 的上级管理者。"""
    mgrs_q = await db.execute(
        select(Agent).where(Agent.id == sender_id)
    )
    mgrs = mgrs_q.scalars().all()
    if not mgrs:
        return []
    mgr = mgrs[0]
    if mgr.manager_agent_id and mgr.manager_agent_id in agent_ids:
        return [mgr.manager_agent_id]
    return []


async def _route_to_collaborators(
    db: AsyncSession, sender_id: str, agent_ids: set[str]
) -> list[str]:
    """路由给与 sender 有协作关系的 agent。"""
    edges_q = await db.execute(
        select(AgentCollaboration)
        .where(AgentCollaboration.source_agent_id == sender_id)
        .where(AgentCollaboration.collaboration_type == CollaborationType.COLLABORATOR)
    )
    edges = edges_q.scalars().all()
    result = [e.target_agent_id for e in edges if e.target_agent_id in agent_ids]
    if not result:
        # 无协作关系时，发给公司内所有 agent（除了自己）
        result = [aid for aid in agent_ids if aid != sender_id]
    return result


async def _route_to_all_collaborators(
    db: AsyncSession, sender_id: str, agent_ids: set[str]
) -> list[str]:
    """路由给公司内所有 agent（除了自己）。"""
    return [aid for aid in agent_ids if aid != sender_id]


async def _route_by_context(
    db: AsyncSession, context_ref: str, sender_id: str, agent_ids: set[str]
) -> list[str]:
    """根据上下文引用路由（如任务、项目、会议）。"""
    from app.models.task import Task as TaskModel
    from app.models.project import Project as ProjectModel
    from app.models.meeting import Meeting as MeetingModel
    from app.models.project import ProjectMember

    result: set[str] = set()

    # 检查是否关联到任务
    try:
        task = await db.get(TaskModel, context_ref)
        if task and task.created_by and task.created_by in agent_ids:
            result.add(task.created_by)
        if task.created_by and task.created_by != sender_id:
            result.add(task.created_by)
    except Exception:
        pass

    # 检查是否关联到项目成员
    try:
        members_q = await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == context_ref)
        )
        members = members_q.scalars().all()
        for m in members:
            if m.agent_id in agent_ids and m.agent_id != sender_id:
                result.add(m.agent_id)
    except Exception:
        pass

    if not result:
        result = agent_ids - {sender_id}

    return list(result)


async def send_collaboration_message(
    db: AsyncSession,
    sender_id: str,
    message_type: CollaborationMessageType,
    title: str,
    content: str,
    company_id: str,
    context_ref: str | None = None,
    context_type: str | None = None,
    receiver_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> AgentMessage:
    """创建并路由一条协作消息。"""
    from app.models.agent import Agent as AgentModel
    from app.models.company import Company as CompanyModel
    import uuid

    sender = await db.get(AgentModel, sender_id)
    if not sender:
        raise ValueError(f"Sender agent {sender_id} not found")

    company = await db.get(CompanyModel, company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    msg = AgentMessage(
        id=str(uuid.uuid4()),
        sender_agent_id=sender_id,
        message_type=message_type,
        title=title,
        content=content,
        context_ref=context_ref,
        context_type=context_type,
        msg_meta=metadata,
        receiver_agent_ids=receiver_ids or [],
    )

    # 路由消息确定接收者
    await route_message(db, msg, sender, company)

    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    return msg
