"""安全面 API（V6 M5）：自然人↔Agent 绑定、审计导出。"""
from __future__ import annotations

import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.agent import Agent
from app.models.bindings import RELATIONS, MemberAgentBinding
from app.models.user import User

bindings_router = APIRouter(prefix="/member-agents", tags=["security"])
audit_router = APIRouter(prefix="/audit", tags=["security"])


async def _owned_guard(db: AsyncSession, user: User, company_id: str,
                 cap: Capability = Capability.VIEW) -> None:
    await assert_company_capability(db, user.id, company_id, cap)


@bindings_router.get("")
async def list_bindings(company_id: str = Query(...),
                        user_id: str | None = Query(None),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """某人（或全员）名下的 Agent 绑定（「我的员工」页数据源）。"""
    await _owned_guard(db, user, company_id)
    q = select(MemberAgentBinding, Agent).join(
        Agent, Agent.id == MemberAgentBinding.agent_id).where(
        MemberAgentBinding.company_id == company_id)
    if user_id:
        q = q.where(MemberAgentBinding.user_id == user_id)
    rows = (await db.execute(q.order_by(MemberAgentBinding.created_at))).all()
    return {"bindings": [{
        "id": b.id, "user_id": b.user_id, "agent_id": b.agent_id,
        "relation": b.relation, "agent_name": a.name, "agent_role": a.role,
        "agent_status": str(a.status.value),
    } for b, a in rows]}


class BindingIn(BaseModel):
    user_id: str
    bindings: list[dict] = Field(default_factory=list, max_length=100)


@bindings_router.put("")
async def set_bindings(body: BindingIn, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """整表设置某自然人的绑定（[{agent_id, relation}]）。"""
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    for item in body.bindings:
        relation = str(item.get("relation") or "subordinate")
        if relation not in RELATIONS:
            raise HTTPException(400, f"relation 须为 {'/'.join(RELATIONS)}")
    # 校验所有 agent 属于本公司
    agent_ids = [str(i.get("agent_id") or "") for i in body.bindings]
    if agent_ids:
        rows = (await db.execute(
            select(Agent.id).where(Agent.company_id == company_id))).scalars().all()
        unknown = [a for a in agent_ids if a not in set(rows)]
        if unknown:
            raise HTTPException(400, f"员工不属于本公司：{unknown[:5]}")
    from sqlalchemy import delete as _delete
    await db.execute(_delete(MemberAgentBinding).where(
        MemberAgentBinding.company_id == company_id,
        MemberAgentBinding.user_id == body.user_id))
    for item in body.bindings:
        db.add(MemberAgentBinding(
            id=str(uuid.uuid4()), company_id=company_id,
            user_id=body.user_id, agent_id=str(item["agent_id"]),
            relation=str(item.get("relation") or "subordinate"),
            created_by=user.id))
    await db.commit()
    return {"ok": True, "count": len(body.bindings)}


@bindings_router.delete("/{binding_id}")
async def delete_binding(binding_id: str, company_id: str = Query(...),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    row = await db.get(MemberAgentBinding, binding_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "绑定不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@audit_router.get("/export")
async def export_audit(company_id: str = Query(...),
                       limit: int = Query(10000, le=50000),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """审计导出（CSV）：event_log 按时间倒序；MANAGE 闸门。

    留存策略与字段级导出归 M5 后续（当前提供全量窗口导出）。
    """
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    from app.models.event import EventLog
    rows = (await db.execute(
        select(EventLog).where(EventLog.company_id == company_id)
        .order_by(EventLog.created_at.desc()).limit(limit))).scalars().all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "event_type", "entity_type", "entity_id",
                     "actor_type", "actor_id", "payload"])
    for r in rows:
        writer.writerow([
            r.created_at.isoformat() if r.created_at else "",
            r.event_type, r.entity_type or "", r.entity_id or "",
            r.actor_type or "", r.actor_id or "",
            str(r.payload or "")])
    filename = f"audit-{company_id[:8]}-{rows[0].created_at:%Y%m%d}" \
        if rows else f"audit-{company_id[:8]}"
    return Response(
        content=buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}.csv"'})
