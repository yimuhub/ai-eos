"""工具总线管理 API（V6 M1）：动态工具登记、授权、健康检查与一键导入。

权限口径（与 plugins.py 一致）：读 = VIEW；登记/启停/授权 = MANAGE；
试执行 = OPERATE（这是真执行，只是调用方是人）。所有入口先校验公司归属，
Agent 授权前校验员工属于该公司。

工具名规则：`^[a-z][a-z0-9_-]{2,119}$`（模型 function-calling 兼容 + 文本
tool_call 解析器的 `[A-Za-z_][A-Za-z0-9_]*` 都吃得下），且不得与 in-code
工具或技能 slug 撞名——总线派发顺序里它们优先，撞名 = 静默遮蔽。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.agent import Agent
from app.models.company_secret import CompanySecret
from app.models.toolbus import AgentToolGrant, ToolRegistry
from app.models.user import User
from app.toolbus import all_incode_tools, execute_bus_tool, get_tool
from app.toolbus.dispatch import mcp_list_tools

router = APIRouter(prefix="/toolbus", tags=["toolbus"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,119}$")
_MAX_IMPORT_TOOLS = 50


async def _owned_guard(db: AsyncSession, user: User, company_id: str,
                 cap: Capability = Capability.VIEW) -> None:
    await assert_company_capability(db, user.id, company_id, cap)


async def _load_row(db: AsyncSession, company_id: str,
                    tool_id: str) -> ToolRegistry:
    row = await db.get(ToolRegistry, tool_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "工具不存在")
    return row


def _row_out(row: ToolRegistry) -> dict:
    return {
        "id": row.id, "name": row.name,
        "display_name": row.display_name, "description": row.description,
        "source": row.source, "source_ref": row.source_ref,
        "package_key": row.package_key, "category": row.category,
        "parameters": row.parameters, "risk": row.risk,
        "required_capability": row.required_capability,
        "timeout_seconds": row.timeout_seconds, "enabled": row.enabled,
        "endpoint": row.endpoint, "auth_secret_ref": row.auth_secret_ref,
        "health_status": row.health_status,
        "last_health_error": row.last_health_error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _spec_out(spec) -> dict:
    return {"name": spec.name, "source": spec.source, "risk": spec.risk,
            "category": spec.category,
            "display_name": spec.display_name or spec.name,
            "description": spec.description,
            "required_capability": spec.required_capability}


# ----------------------------------------------------------------- 目录


@router.get("/inventory")
async def inventory(company_id: str = Query(...),
                    db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """工具总目录：in-code（web/电脑/服务端）+ 本公司动态登记。"""
    await _owned_guard(db, user, company_id)
    rows = (await db.execute(
        select(ToolRegistry).where(ToolRegistry.company_id == company_id)
        .order_by(ToolRegistry.created_at))).scalars().all()
    builtin = [_spec_out(s) for s in all_incode_tools() if s.source == "builtin"]
    computer = [_spec_out(s) for s in all_incode_tools() if s.source == "computer"]
    server = [_spec_out(s) for s in all_incode_tools() if s.source == "mcp_server"]
    return {"builtin": builtin, "computer": computer, "server": server,
            "registered": [_row_out(r) for r in rows]}


class ToolRegisterIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    display_name: str | None = None
    description: str | None = None
    source: str = "custom"          # mcp_http | connector | custom
    source_ref: str | None = None   # 外部 MCP 的真实工具名（默认 = name）
    package_key: str | None = None
    category: str | None = None
    parameters: dict | None = None
    risk: str = "low"               # low | medium | high
    required_capability: str = "operate"
    timeout_seconds: int = 60
    endpoint: str | None = None
    auth_secret_ref: str | None = None


@router.post("/tools", status_code=201)
async def register_tool(body: ToolRegisterIn,
                        company_id: str = Query(...),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    import uuid

    if not _NAME_RE.match(body.name or ""):
        raise HTTPException(400, "工具名须为 3~120 位小写字母开头，"
                                 "只含 a-z 0-9 _ -")
    if get_tool(body.name):
        raise HTTPException(400, f"名字与内置工具冲突：{body.name}")
    from app.skill_system import get_skill_class
    if get_skill_class(body.name):
        raise HTTPException(400, f"名字与技能 slug 冲突：{body.name}")
    if body.source not in ("mcp_http", "connector", "custom"):
        raise HTTPException(400, "source 仅支持 mcp_http / connector / custom")
    if body.source == "mcp_http":
        if not (body.endpoint or "").startswith(("http://", "https://")):
            raise HTTPException(400, "mcp_http 工具必须提供 http/https endpoint")
        # 登记档闸门：语法 + 字面 IP（DNS 解析留给执行档，登记时可能离线）
        from app.toolbus.dispatch import _assert_public_http_url
        try:
            _assert_public_http_url(body.endpoint, resolve=False)
        except ValueError as exc:
            raise HTTPException(400, f"endpoint 被拒绝：{exc}") from exc
    if body.risk not in ("low", "medium", "high"):
        raise HTTPException(400, "risk 仅支持 low / medium / high")
    if body.auth_secret_ref:
        secret = (await db.execute(
            select(CompanySecret.id).where(
                CompanySecret.company_id == company_id,
                CompanySecret.name == body.auth_secret_ref))).first()
        if secret is None:
            raise HTTPException(400,
                                f"凭据 {body.auth_secret_ref} 不存在，"
                                "请先在 BYOK 保险库里录入")
    dup = (await db.execute(
        select(ToolRegistry.id).where(
            ToolRegistry.company_id == company_id,
            ToolRegistry.name == body.name))).first()
    if dup:
        raise HTTPException(400, f"该公司已登记同名工具：{body.name}")

    row = ToolRegistry(
        id=str(uuid.uuid4()), company_id=company_id, name=body.name,
        display_name=body.display_name, description=body.description,
        source=body.source, source_ref=body.source_ref,
        package_key=body.package_key, category=body.category,
        parameters=body.parameters or {"type": "object", "properties": {}},
        risk=body.risk, required_capability=body.required_capability,
        timeout_seconds=max(5, min(int(body.timeout_seconds or 60), 600)),
        endpoint=body.endpoint, auth_secret_ref=body.auth_secret_ref,
        created_by=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    from app.services.event_system import log_event
    await log_event(db, "tool.registered", "tool", row.name, "user", user.id,
                    {"source": row.source, "risk": row.risk},
                    company_id=company_id)
    await db.commit()
    return {"ok": True, "tool": _row_out(row)}


@router.patch("/tools/{tool_id}")
async def toggle_tool(tool_id: str, enabled: bool = Query(...),
                      company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    row = await _load_row(db, company_id, tool_id)
    row.enabled = enabled
    await db.commit()
    return {"ok": True, "tool": _row_out(row)}


@router.delete("/tools/{tool_id}")
async def unregister_tool(tool_id: str, company_id: str = Query(...),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    row = await _load_row(db, company_id, tool_id)
    # 授权行按名字挂的：撤销登记时一并清掉，避免残留死授权
    await db.execute(delete(AgentToolGrant).where(
        AgentToolGrant.company_id == company_id,
        AgentToolGrant.tool_name == row.name))
    await db.delete(row)
    await db.commit()
    return {"ok": True, "message": f"已撤销登记 {row.name}（授权一并清除）"}


@router.post("/tools/{tool_id}/health")
async def check_health(tool_id: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """健康检查：mcp_http 工具真实探测端点 tools/list；其它来源标 unknown。"""
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    row = await _load_row(db, company_id, tool_id)
    if row.source != "mcp_http" or not row.endpoint:
        row.health_status = "unknown"
        row.last_health_error = None
        row.last_health_at = datetime.utcnow()
        await db.commit()
        return {"ok": True, "status": "unknown",
                "message": "该来源暂无主动探测，登记即视为可用"}
    try:
        bearer = None
        if row.auth_secret_ref:
            from app.toolbus.dispatch import _resolve_bearer
            bearer = await _resolve_bearer(company_id, row.auth_secret_ref)
        tools = await mcp_list_tools(row.endpoint, bearer=bearer,
                                     timeout=float(row.timeout_seconds or 60))
        row.health_status = "ok"
        row.last_health_error = None
        row.last_health_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        return {"ok": True, "status": "ok", "remote_tools": len(tools)}
    except Exception as exc:  # noqa: BLE001 — 探测失败就是健康失败
        row.health_status = "error"
        row.last_health_error = f"{type(exc).__name__}: {exc}"[:300]
        row.last_health_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        return {"ok": False, "status": "error",
                "message": row.last_health_error}


@router.post("/tools/{tool_id}/import")
async def import_remote_tools(tool_id: str, company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """一键导入：把外部 MCP Server 的 tools/list 全部登记为本公司工具。

    每个远端工具登记一行：name = 前缀_远端名（避免撞名），source_ref =
    远端真实工具名（执行时按它 tools/call）。重复跳过，上限 50 个。
    """
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    import uuid
    row = await _load_row(db, company_id, tool_id)
    if row.source != "mcp_http" or not row.endpoint:
        raise HTTPException(400, "只有 mcp_http 工具支持一键导入")
    bearer = None
    if row.auth_secret_ref:
        from app.toolbus.dispatch import _resolve_bearer
        bearer = await _resolve_bearer(company_id, row.auth_secret_ref)
    try:
        remote = await mcp_list_tools(row.endpoint, bearer=bearer,
                                      timeout=float(row.timeout_seconds or 60))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"端点探测失败：{type(exc).__name__}: "
                                 f"{str(exc)[:200]}") from exc

    prefix = re.sub(r"[^a-z0-9_-]+", "_", row.name.lower()).strip("_")
    existing = set((await db.execute(
        select(ToolRegistry.name).where(
            ToolRegistry.company_id == company_id))).scalars().all())
    added, skipped = [], []
    for t in remote[:_MAX_IMPORT_TOOLS]:
        tname = str(t.get("name") or "").strip()
        if not tname:
            continue
        safe = re.sub(r"[^a-z0-9_-]+", "_", tname.lower()).strip("_") or "tool"
        new_name = f"{prefix}_{safe}"[:120]
        if new_name in existing or get_tool(new_name):
            skipped.append(tname)
            continue
        db.add(ToolRegistry(
            id=str(uuid.uuid4()), company_id=company_id, name=new_name,
            display_name=t.get("description", "")[:200] or tname,
            description=t.get("description") or tname,
            source="mcp_http", source_ref=tname,
            package_key=row.package_key, category=row.category,
            parameters=t.get("inputSchema")
            or {"type": "object", "properties": {}},
            risk=row.risk, required_capability=row.required_capability,
            timeout_seconds=row.timeout_seconds, endpoint=row.endpoint,
            auth_secret_ref=row.auth_secret_ref, created_by=user.id))
        existing.add(new_name)
        added.append({"remote": tname, "as": new_name})
    await db.commit()
    return {"ok": True, "added": added, "skipped": skipped,
            "message": f"导入 {len(added)} 个工具，跳过 {len(skipped)} 个"}


@router.post("/tools/{tool_id}/test")
async def test_execute(tool_id: str, company_id: str = Query(...),
                       arguments: dict | None = None,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """试执行（真执行，记审计）：给人用的"装好后点一下试试"。"""
    await _owned_guard(db, user, company_id, Capability.OPERATE)
    row = await _load_row(db, company_id, tool_id)
    if not row.enabled:
        raise HTTPException(400, "工具已停用，先启用再试")
    res = await execute_bus_tool(row.name, arguments or {},
                                 company_id=company_id, user_id=user.id)
    return {"ok": "error" not in res, "result": res}


# ----------------------------------------------------------------- 授权


@router.get("/agents/{agent_id}/tools")
async def list_agent_tools(agent_id: str, company_id: str = Query(...),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    await _owned_guard(db, user, company_id)
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.company_id != company_id:
        raise HTTPException(404, "员工不存在")
    from app.toolbus import resolve_agent_tools
    specs = await resolve_agent_tools(db, agent)
    grants = (await db.execute(
        select(AgentToolGrant.tool_name).where(
            AgentToolGrant.agent_id == agent_id))).scalars().all()
    return {"granted": sorted(grants),
            "resolved": [{"name": s.name, "source": s.source,
                          "risk": s.risk,
                          "display_name": s.display_name or s.name}
                         for s in specs]}


class GrantIn(BaseModel):
    tool_names: list[str] = Field(default_factory=list, max_length=200)


@router.put("/agents/{agent_id}/tools")
async def set_agent_tools(agent_id: str, body: GrantIn,
                          company_id: str = Query(...),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """整表设置某员工授权（前端一次提交全量名单；内置工具无需授权）。"""
    await _owned_guard(db, user, company_id, Capability.MANAGE)
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.company_id != company_id:
        raise HTTPException(404, "员工不存在")
    # 校验名单：只允许公司已登记的动态工具（内置/电脑工具由能力包决定，
    # 不进授权表，避免两套口径）
    if body.tool_names:
        registered = set((await db.execute(
            select(ToolRegistry.name).where(
                ToolRegistry.company_id == company_id))).scalars().all())
        unknown = [n for n in body.tool_names if n not in registered]
        if unknown:
            raise HTTPException(400, f"未登记的工具：{', '.join(unknown[:8])}")
    await db.execute(delete(AgentToolGrant).where(
        AgentToolGrant.agent_id == agent_id))
    for n in dict.fromkeys(body.tool_names):  # 去重保序
        db.add(AgentToolGrant(company_id=company_id, agent_id=agent_id,
                              tool_name=n, granted_by=user.id))
    await db.commit()
    from app.services.event_system import log_event
    await log_event(db, "tool.grants_updated", "agent", agent_id, "user",
                    user.id, {"count": len(body.tool_names)},
                    company_id=company_id)
    await db.commit()
    return {"ok": True, "granted": sorted(set(body.tool_names)),
            "message": f"已更新授权（{len(set(body.tool_names))} 个工具）"}
