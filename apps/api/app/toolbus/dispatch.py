"""工具总线分发器：唯一的工具执行入口。

路由顺序：in-code 执行器（web/computer）→ DB 动态工具（mcp_http 直通等）
→ 结构化错误。所有调用（含失败与被拒绝的）落 `tool.call` 审计事件；
结果截断到 MAX_TOOL_RESULT_CHARS 后回填模型上下文。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

from app.core.logging_config import get_logger
from .registry import (MAX_TOOL_RESULT_CHARS, ToolContext, get_tool)

logger = get_logger(__name__)


def _truncate(value, limit: int = MAX_TOOL_RESULT_CHARS):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


# ---------------------------------------------------------------------------
# DB 动态工具执行
# ---------------------------------------------------------------------------

def _assert_public_http_url(url: str, *, resolve: bool = True) -> None:
    """MCP endpoint 的出口闸门：只允许公网 http/https + 本机回环。

    resolve=False 是登记档：只做语法与字面 IP 检查（登记时 DNS 可能还
    不可用）；执行/健康检查/导入档 resolve=True 再做真实解析。
    回环（127.0.0.1/localhost/::1）刻意放行：便携单机部署里，同机的
    本地 MCP Server 是最主要的接入形态。其余私网段（10/172.16/192.168）、
    链路本地与云元数据地址拒绝——与 web_fetch 的 SSRF 闸门同一威胁模型。
    DNS rebinding 的 TOCTOU 加固后续统一在 http_client 层做；
    局域网 MCP 白名单放开归 M5 安全档。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("MCP endpoint 只允许 http/https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("MCP endpoint 缺少主机名")
    if host in ("localhost", "0.0.0.0") or host.endswith(".localhost"):
        return  # 本机服务，放行
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _check_ip(literal_ip)
        return
    if not resolve:
        return  # 域名在登记档不解析，执行档再查
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if
                              parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"endpoint 主机无法解析: {exc}") from exc
    for _family, _type, _proto, _canonname, sockaddr in infos:
        _check_ip(ipaddress.ip_address(sockaddr[0]))


def _check_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> None:
    if ip.is_loopback:
        return  # 解析到回环 = 本机服务，放行
    if (ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast):
        raise ValueError(f"endpoint 命中内网/保留地址 {ip}，已拒绝")


async def _resolve_bearer(company_id: str, secret_ref: str | None) -> str | None:
    """从公司保险库取 bearer 凭据；引用不存在/解密失败都返回 None 并告警。"""
    if not secret_ref:
        return None
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.company_secret import CompanySecret
    from app.services.secret_vault import decrypt
    async with AsyncSessionLocal() as sdb:
        row = (await sdb.execute(
            select(CompanySecret).where(
                CompanySecret.company_id == company_id,
                CompanySecret.name == secret_ref))).scalars().first()
    if row is None:
        logger.warning("工具凭据引用不存在 company=%s ref=%s",
                       company_id, secret_ref)
        return None
    try:
        return decrypt(row.ciphertext)
    except Exception:  # noqa: BLE001 — 凭据损坏不应把工具调用变成 500
        logger.exception("工具凭据解密失败 company=%s ref=%s",
                         company_id, secret_ref)
        return None


async def execute_db_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    """执行 tool_registry 里登记的动态工具。"""
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.toolbus import ToolRegistry

    if not ctx.company_id:
        # 不带公司上下文时，动态工具一律不可见（消息与未知工具一致，
        # 兼容 app.tools 历史契约）
        return {"error": f"未知工具: {name}"}
    async with AsyncSessionLocal() as tdb:
        row = (await tdb.execute(
            select(ToolRegistry).where(
                ToolRegistry.company_id == ctx.company_id,
                ToolRegistry.name == name))).scalars().first()
        if row is None:
            return {"error": f"未知工具: {name}"}
        if not row.enabled:
            return {"error": f"工具 {name} 已停用"}
        if row.source == "mcp_http":
            endpoint = (row.endpoint or "").strip()
            try:
                _assert_public_http_url(endpoint)
            except ValueError as exc:
                return {"error": f"endpoint 被拒绝：{exc}"}
            bearer = await _resolve_bearer(str(ctx.company_id),
                                           row.auth_secret_ref)
            from app.api.routes.mcp import mcp_http_execute
            task_input = json.dumps({
                "tool": row.source_ref or row.name,
                "arguments": args}, ensure_ascii=False)
            res = await mcp_http_execute(
                endpoint, task_input, row.config, bearer=bearer,
                timeout=float(row.timeout_seconds or 60))
            return res.get("result") or res
        # connector / custom / mcp_stdio：M3 连接器 SDK 与子进程运行时接手
        return {"error": f"来源 {row.source} 的工具暂不支持执行（等待连接器 SDK）"}


# ---------------------------------------------------------------------------
# MCP endpoint 探测（健康检查 / 一键导入共用）
# ---------------------------------------------------------------------------

async def mcp_list_tools(endpoint: str, *, bearer: str | None = None,
                         timeout: float = 15.0) -> list[dict]:
    """调外部 MCP Server 的 tools/list，返回工具描述清单（空列表 = 端点活着但没工具）。

    endpoint 先过公网闸门；HTTP 错误向上抛（调用方转成健康失败）。
    """
    _assert_public_http_url(endpoint)
    from app.core.http_client import http_client

    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    def _rpc(method: str, params: dict | None = None, rid=1):
        return {"jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {}}

    async with http_client(timeout=timeout) as client:
        r = await client.post(endpoint, json=_rpc("tools/list"),
                              headers=headers)
        r.raise_for_status()
        if "text/event-stream" in r.headers.get("content-type", ""):
            line = next((ln for ln in r.text.splitlines()
                         if ln.startswith("data:")), "{}")
            data = json.loads(line[len("data:"):].strip() or "{}")
        else:
            data = r.json()
        return list(((data.get("result") or {}).get("tools") or []))


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

async def _audit(name: str, ctx: ToolContext, *, ok: bool, source: str,
                 risk: str, duration_ms: int, error: str | None,
                 dlp_findings: list[dict] | None = None) -> None:
    """tool.call 审计：独立短会话，失败只告警不拖垮工具调用本身。"""
    try:
        from app.db.session import AsyncSessionLocal
        from app.services.event_system import log_event
        payload = {"ok": ok, "source": source, "risk": risk,
                   "duration_ms": duration_ms, "task_id": ctx.task_id}
        if error:
            payload["error"] = error[:200]
        if dlp_findings:
            payload["dlp_findings"] = dlp_findings[:10]
        async with AsyncSessionLocal() as adb:
            await log_event(adb, "tool.call", "tool", name,
                            "agent" if ctx.agent_id else
                            ("user" if ctx.user_id else "system"),
                            ctx.agent_id or ctx.user_id,
                            payload, company_id=ctx.company_id)
            await adb.commit()
    except Exception:  # noqa: BLE001
        logger.exception("工具审计写入失败 tool=%s", name)


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------

async def execute_bus_tool(name: str, args, *, company_id: str | None = None,
                           agent_id: str | None = None,
                           user_id: str | None = None,
                           task_id: str | None = None) -> dict:
    """执行一个工具。错误一律转成结构化结果返回给模型，不抛给运行时。

    抛异常会让整个任务失败，而模型完全有能力在"这次抓取失败"后换一个
    来源；把它变成 tool 消息里的 error 字段才是正确的降级方式。
    """
    args = args if isinstance(args, dict) else {}
    ctx = ToolContext(company_id=company_id, agent_id=agent_id,
                      user_id=user_id, task_id=task_id, tool_name=name)
    spec = get_tool(name)
    source = spec.source if spec else "db"
    risk = spec.risk if spec else "medium"
    started = time.monotonic()
    try:
        if spec is not None and spec.executor is not None:
            result = await spec.executor(args, ctx)
        elif spec is not None and spec.source == "mcp_server":
            result = {"error": "aieos.* 服务端工具只面向外部 Agent（MCP 通道），"
                               "不在员工侧执行"}
        else:
            result = await execute_db_tool(name, args, ctx)
    except Exception as exc:  # 工具失败不拖垮任务
        logger.warning("工具执行失败 name=%s: %s", name, exc, exc_info=True)
        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    ok = isinstance(result, dict) and "error" not in result
    # 出站 DLP（V6 M5）：结果进模型上下文前过脱敏闸门；公司可用功能开关
    # dlp:outbound=disabled 关闭（明配置，不静默）。命中情况进审计。
    dlp_findings: list[dict] = []
    if ok:
        result, dlp_findings = await _maybe_mask(result, ctx)
    await _audit(name, ctx, ok=ok, source=source, risk=risk,
                 duration_ms=int((time.monotonic() - started) * 1000),
                 error=None if ok else str(result.get("error", ""))[:200],
                 dlp_findings=dlp_findings)
    return _truncate(result)


async def _maybe_mask(result, ctx: ToolContext):
    """按公司开关执行出站脱敏；开关查询失败按开启处理（fail-safe）。"""
    try:
        from sqlalchemy import select as _select
        from app.db.session import AsyncSessionLocal
        from app.models.feature_flags import CompanyFeatureFlag
        async with AsyncSessionLocal() as fdb:
            if ctx.company_id:
                row = (await fdb.execute(
                    _select(CompanyFeatureFlag).where(
                        CompanyFeatureFlag.company_id == ctx.company_id,
                        CompanyFeatureFlag.key == "dlp:outbound"))).scalars().first()
                if row is not None and not row.enabled:
                    return result, []
        from app.services.dlp import mask_result
        return mask_result(result)
    except Exception:  # noqa: BLE001 — 脱敏层故障不拖垮工具调用
        logger.exception("DLP 脱敏检查失败 tool=%s", ctx.tool_name)
        return result, []
