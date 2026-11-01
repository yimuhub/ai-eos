"""External Agent Integration API (P3-09).

安全约束：
* `auth_env_var` 只允许模型服务密钥白名单（见 app.external_agents），
  拒绝 JWT_SECRET / *_SECRET / *_PASSWORD 等服务端敏感变量。
* `endpoint` 一律过出站闸门（allow_private=True，本地 LLM 可用，元数据拒绝）。
* 所有按 id 的端点都校验公司归属，越权一律 404。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.external_agents import (ADAPTERS, AgentProviderType,
                                 ConnectionStatus, ExternalAgentConnection,
                                 get_adapter, validate_auth_env_var)
from app.models.user import User

router = APIRouter(prefix="/external-agents", tags=["external-agents"])


class ConnectionIn(BaseModel):
    name: str
    provider_type: AgentProviderType
    endpoint: str | None = None
    auth_env_var: str | None = None      # 环境变量名，非明文 key
    capabilities: list[str] | None = None
    attachments: list[dict] | None = None  # 📎 附件口


async def _validate_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return endpoint
    try:
        await validate_outbound_url_async(endpoint, allow_private=True)
    except OutboundURLRejected as e:
        raise HTTPException(400, f"endpoint 不安全：{e}")
    return endpoint


def _validate_env_var(name: str | None) -> str | None:
    try:
        return validate_auth_env_var(name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("", status_code=201)
async def register(
    data: ConnectionIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.provider_type not in ADAPTERS:
        raise HTTPException(400, "invalid provider_type")
    # 不能凭一个公司 id 就往别人公司里注册外部 Agent
    await assert_company_owned(db, user.id, company_id)
    # 先校验密钥引用名（纯字符串判断、不依赖 DNS），再校验 endpoint
    auth_env_var = _validate_env_var(data.auth_env_var)
    endpoint = await _validate_endpoint(data.endpoint)
    conn = ExternalAgentConnection(
        id=str(__import__("uuid").uuid4()), company_id=company_id,
        name=data.name, provider_type=data.provider_type,
        endpoint=endpoint, auth_env_var=auth_env_var,
        capabilities=data.capabilities or [],
        attachments=data.attachments or [],
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
    await assert_company_owned(db, user.id, company_id)
    q = await db.execute(select(ExternalAgentConnection).where(
        ExternalAgentConnection.company_id == company_id).order_by(
        ExternalAgentConnection.created_at.desc()))
    rows = q.scalars().all()
    return [{"id": c.id, "name": c.name, "provider": c.provider_type.value,
             "endpoint": c.endpoint, "auth_env_var": c.auth_env_var,
             "capabilities": c.capabilities, "status": c.status.value,
             "attachments": c.attachments or [],
             "last_test_ok": c.last_test_ok} for c in rows]


async def _get_owned_connection(db: AsyncSession, user: User,
                                cid: str) -> ExternalAgentConnection:
    conn = await db.get(ExternalAgentConnection, cid)
    if not conn:
        raise HTTPException(404, "not found")
    await assert_company_owned(db, user.id, conn.company_id)
    return conn


@router.post("/{cid}/test")
async def test_connection(
    cid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conn = await _get_owned_connection(db, user, cid)
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
    conn = await _get_owned_connection(db, user, cid)
    conn.status = ConnectionStatus.ENABLED
    await db.commit()
    return {"id": cid, "status": "ENABLED"}


@router.post("/{cid}/disable")
async def disable(cid: str, db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    conn = await _get_owned_connection(db, user, cid)
    conn.status = ConnectionStatus.DISABLED
    await db.commit()
    return {"id": cid, "status": "DISABLED"}


@router.delete("/{cid}")
async def delete_connection(cid: str, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    conn = await _get_owned_connection(db, user, cid)
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
    """通过统一 Adapter 执行外部 Agent（业务代码不直接绑定 Provider）。

    V5.5 A3 审批门：公司策略开启「外部 Agent 外发」审批时，
    先建 external_agent 审批单挂起，老板批准后自动执行。
    """
    conn = await _get_owned_connection(db, user, cid)
    if conn.status != ConnectionStatus.ENABLED:
        raise HTTPException(409, f"连接未启用（{conn.status.value}）")
    if await _external_send_needs_approval(db, conn.company_id):
        import uuid
        from app.models.intervention import ApprovalRequest
        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            company_id=conn.company_id,
            action_type="external_agent",
            payload={"connection_id": conn.id,
                     "connection_name": conn.name,
                     "task_input": data.task_input,
                     "config": data.config,
                     "awaiting_execution": True},
            reason=(f"外部 Agent「{conn.name}」收到外发执行请求："
                    f"{(data.task_input or '')[:100]}"))
        db.add(req)
        from app.services.notification_service import notify_approval_pending
        await notify_approval_pending(
            db, company_id=conn.company_id,
            title=f"待审批：外部 Agent「{conn.name}」外发",
            body=req.reason)
        await db.commit()
        return {"status": "pending_approval", "approval_id": req.id,
                "message": "已生成审批单，等待老板批准后执行"}
    out = await _run_external(conn, data, db)
    if "_error" in out:
        code = out.pop("_code", 500)
        raise HTTPException(code, out.pop("_error"))
    return out


async def _run_external(conn, data: "ExecuteIn", db: AsyncSession) -> dict:
    """真正执行外部 Agent（A3 抽离：审批批准后复用同一入口）。"""
    try:
        adapter = get_adapter(conn)
        result = await adapter.execute(data.task_input, data.config)
    except ValueError as e:
        return {"_error": str(e), "_code": 400}
    except Exception as e:
        return {"_error": f"执行失败: {str(e)[:200]}", "_code": 502}
    return {"connection": conn.name, **result}


async def _external_send_needs_approval(db: AsyncSession, company_id: str) -> bool:
    """公司审批策略是否要求「外部 Agent 外发」过审批门。"""
    from sqlalchemy import select
    from app.models.intervention import InterventionPolicy
    pol = (await db.execute(
        select(InterventionPolicy).where(
            InterventionPolicy.company_id == company_id,
            InterventionPolicy.level >= 1))).scalars().first()
    if not pol:
        return False
    acts = pol.approval_actions or []
    return "external_agent" in acts
