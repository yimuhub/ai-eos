"""External Agent Integration API (P3-09)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.external_agents import (ADAPTERS, AgentProviderType,
                                 ConnectionStatus, ExternalAgentConnection,
                                 get_adapter)
from app.models.user import User

router = APIRouter(prefix="/external-agents", tags=["external-agents"])


class ConnectionIn(BaseModel):
    name: str
    provider_type: AgentProviderType
    endpoint: str | None = None
    auth_env_var: str | None = None      # 环境变量名，非明文 key
    capabilities: list[str] | None = None


@router.post("", status_code=201)
async def register(
    data: ConnectionIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.provider_type not in ADAPTERS:
        raise HTTPException(400, "invalid provider_type")
    conn = ExternalAgentConnection(
        id=str(__import__("uuid").uuid4()), company_id=company_id,
        name=data.name, provider_type=data.provider_type,
        endpoint=data.endpoint, auth_env_var=data.auth_env_var,
        capabilities=data.capabilities or [],
        supported_actions=["execute", "get_status", "pause", "resume",
                           "cancel"])
    db.add(conn)
    await db.commit()
    return {"id": conn.id, "name": conn.name,
            "provider": conn.provider_type.value}


@router.get("")
async def list_connections(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(ExternalAgentConnection).where(
        ExternalAgentConnection.company_id == company_id).order_by(
        ExternalAgentConnection.created_at.desc()))
    rows = q.scalars().all()
    return [{"id": c.id, "name": c.name, "provider": c.provider_type.value,
             "endpoint": c.endpoint, "auth_env_var": c.auth_env_var,
             "capabilities": c.capabilities, "status": c.status.value,
             "last_test_ok": c.last_test_ok} for c in rows]


@router.post("/{cid}/test")
async def test_connection(
    cid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    if conn.provider_type == AgentProviderType.NATIVE:
        result = {"ok": True, "note": "native agent"}
    else:
        adapter = get_adapter(conn)
        try:
            result = await adapter.test_connection()
        except Exception as e:
            result = {"ok": False, "error": str(e)[:150]}
    from datetime import datetime, timezone
    conn.last_test_at = datetime.now(timezone.utc).replace(tzinfo=None)
    conn.last_test_ok = result.get("ok")
    await db.commit()
    return result


@router.post("/{cid}/enable")
async def enable(cid: str, db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    conn.status = ConnectionStatus.ENABLED
    await db.commit()
    return {"id": cid, "status": "ENABLED"}


@router.post("/{cid}/disable")
async def disable(cid: str, db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    conn.status = ConnectionStatus.DISABLED
    await db.commit()
    return {"id": cid, "status": "DISABLED"}


@router.delete("/{cid}")
async def delete_connection(cid: str, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    await db.delete(conn)
    await db.commit()
    return {"deleted": True}


class ExecuteIn(BaseModel):
    task_input: str
    config: dict | None = None


@router.post("/{cid}/execute")
async def execute(cid: str, data: ExecuteIn,
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """通过统一 Adapter 执行外部 Agent（业务代码不直接绑定 Provider）。"""
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    if conn.status != ConnectionStatus.ENABLED:
        raise HTTPException(409, f"连接未启用（{conn.status.value}）")
    try:
        adapter = get_adapter(conn)
        result = await adapter.execute(data.task_input, data.config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"执行失败: {str(e)[:200]}")
    return {"connection": conn.name, **result}
