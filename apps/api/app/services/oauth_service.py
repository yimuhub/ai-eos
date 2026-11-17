"""连接器 OAuth 托管流（V6.1 引擎 2）：授权回调 → token 入保险库 → 自动刷新。

config.oauth 结构（挂在连接器 tool_registry.config.oauth）：
{
  "authorize_url": "https://.../authorize",
  "token_url": "https://.../token",
  "client_id": "...",
  "scopes": ["read", "write"],
  "token_kind": "bearer",            # bearer（refresh_token 标准流）
  "expires_in": 7200                 # 可选；不填以响应为准
}
client_secret 存 company_secrets（名字 = oauth.client_secret_ref）。

token 落库同样走保险库：oauth:{tool_id}:access / :refresh 两行密文。
pull 侧通过 `get_access_token` 取 token：过期自动用 refresh_token 换新。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging_config import get_logger
from app.toolbus.dispatch import _assert_public_http_url

logger = get_logger(__name__)

_STATE_TTL = 600  # 授权回调 state 有效期（秒）


def _sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def build_authorize_url(row, base_redirect: str) -> tuple[str, str]:
    """构造第三方授权页 URL，返回 (url, state)。state 带 HMAC 防篡改。"""
    oauth = dict((row.config or {}).get("oauth") or {})
    authorize_url = str(oauth.get("authorize_url") or "")
    client_id = str(oauth.get("client_id") or "")
    if not authorize_url or not client_id:
        raise ValueError("连接器未配置 oauth.authorize_url/client_id")
    _assert_public_http_url(authorize_url, resolve=False)
    nonce = secrets.token_urlsafe(12)
    state_payload = f"{row.id}:{int(time.time())}:{nonce}"
    state = f"{state_payload}:{_sig(settings.jwt_secret, state_payload)}"
    scopes = oauth.get("scopes") or []
    redirect_uri = f"{base_redirect.rstrip('/')}/connectors/oauth/callback"
    sep = "&" if "?" in authorize_url else "?"
    url = (f"{authorize_url}{sep}response_type=code&client_id={client_id}"
           f"&redirect_uri={redirect_uri}&state={state}"
           + (f"&scope={' '.join(scopes)}" if scopes else ""))
    return url, state


def verify_state(state: str) -> str:
    """校验回调 state（HMAC + 10 分钟时效），返回 tool_id。

    state 形如 `tool_id:ts:nonce:sig`，sig = HMAC(jwt_secret, "tool_id:ts:nonce")。
    """
    parts = (state or "").split(":")
    if len(parts) != 4:
        raise PermissionError("state 格式非法")
    tool_id, ts, nonce, sig = parts
    payload = f"{tool_id}:{ts}:{nonce}"
    if not hmac.compare_digest(_sig(settings.jwt_secret, payload), sig):
        raise PermissionError("state 签名校验失败")
    if time.time() - int(ts) > _STATE_TTL:
        raise PermissionError("state 已过期，请重新发起授权")
    return tool_id


async def exchange_code(db: AsyncSession, row, code: str,
                        redirect_uri: str) -> dict:
    """code 换 token：access/refresh 密文入保险库，返回过期时间戳。"""
    from sqlalchemy import select as _select
    from app.models.company_secret import CompanySecret
    from app.services.secret_vault import decrypt, encrypt
    from app.core.http_client import http_client

    oauth = dict((row.config or {}).get("oauth") or {})
    token_url = str(oauth.get("token_url") or "")
    _assert_public_http_url(token_url)
    client_secret_ref = str(oauth.get("client_secret_ref") or "")
    secret_row = (await db.execute(
        _select(CompanySecret).where(
            CompanySecret.company_id == row.company_id,
            CompanySecret.name == client_secret_ref))).scalars().first() \
        if client_secret_ref else None
    if client_secret_ref and secret_row is None:
        raise ValueError(f"client_secret {client_secret_ref} 不存在")
    client_secret = decrypt(secret_row.ciphertext) if secret_row else None

    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": oauth.get("client_id")}
    if client_secret:
        data["client_secret"] = client_secret
    async with http_client(timeout=30) as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        token = resp.json()
    if "access_token" not in token:
        raise ValueError("token 响应缺少 access_token")
    return await _store_tokens(db, row, token)


async def _store_tokens(db: AsyncSession, row, token: dict) -> dict:
    """access/refresh 密文入保险库；config 记录过期时间戳。"""
    from sqlalchemy import select as _select
    from app.models.company_secret import CompanySecret
    from app.services.secret_vault import encrypt

    expires_in = int(token.get("expires_in") or 0)
    expires_at = int(time.time()) + expires_in if expires_in else None
    items = [("oauth:{id}:access", token.get("access_token")),
             ("oauth:{id}:refresh", token.get("refresh_token"))]
    for name_tpl, value in items:
        if not value:
            continue
        name = name_tpl.format(id=row.id)
        existing = (await db.execute(
            _select(CompanySecret).where(
                CompanySecret.company_id == row.company_id,
                CompanySecret.name == name))).scalars().first()
        ciphertext = encrypt(value)
        if existing:
            existing.ciphertext = ciphertext
        else:
            db.add(CompanySecret(company_id=row.company_id, name=name,
                                 ciphertext=ciphertext))
    cfg = dict(row.config or {})
    oauth_cfg = dict(cfg.get("oauth") or {})
    oauth_cfg["connected"] = True
    if expires_at:
        oauth_cfg["access_expires_at"] = expires_at
    cfg["oauth"] = oauth_cfg
    row.config = cfg
    await db.commit()
    return {"connected": True, "access_expires_at": expires_at,
            "has_refresh": bool(token.get("refresh_token"))}


async def get_access_token(db: AsyncSession, row) -> str | None:
    """pull 侧取 token：未过期直接解密返回；过期且有 refresh_token 自动换新。"""
    from sqlalchemy import select as _select
    from app.models.company_secret import CompanySecret
    from app.services.secret_vault import decrypt, encrypt
    from app.core.http_client import http_client

    oauth = dict((row.config or {}).get("oauth") or {})
    if not oauth.get("connected"):
        return None
    access_name = f"oauth:{row.id}:access"
    row_ = (await db.execute(
        _select(CompanySecret).where(
            CompanySecret.company_id == row.company_id,
            CompanySecret.name == access_name))).scalars().first()
    if row_ is None:
        return None

    expires_at = int(oauth.get("access_expires_at") or 0)
    if expires_at - 60 > time.time():
        return decrypt(row_.ciphertext)

    refresh_name = f"oauth:{row.id}:refresh"
    refresh_row = (await db.execute(
        _select(CompanySecret).where(
            CompanySecret.company_id == row.company_id,
            CompanySecret.name == refresh_name))).scalars().first()
    token_url = str(oauth.get("token_url") or "")
    if refresh_row is None or not token_url:
        logger.warning("OAuth token 已过期且无法刷新 tool=%s", row.name)
        return None
    _assert_public_http_url(token_url)
    client_secret_ref = str(oauth.get("client_secret_ref") or "")
    secret_row = (await db.execute(
        _select(CompanySecret).where(
            CompanySecret.company_id == row.company_id,
            CompanySecret.name == client_secret_ref))).scalars().first() \
        if client_secret_ref else None
    data = {"grant_type": "refresh_token",
            "refresh_token": decrypt(refresh_row.ciphertext),
            "client_id": oauth.get("client_id")}
    if secret_row:
        data["client_secret"] = decrypt(secret_row.ciphertext)
    async with http_client(timeout=30) as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        token = resp.json()
    await _store_tokens(db, row, token)
    access_row = (await db.execute(
        _select(CompanySecret).where(
            CompanySecret.company_id == row.company_id,
            CompanySecret.name == access_name))).scalars().first()
    return decrypt(access_row.ciphertext) if access_row else None


async def disconnect(db: AsyncSession, row) -> None:
    """断开授权：删 token 密文行 + 清 connected 标记。"""
    from sqlalchemy import delete as _delete, select as _select
    from app.models.company_secret import CompanySecret

    await db.execute(_delete(CompanySecret).where(
        CompanySecret.company_id == row.company_id,
        CompanySecret.name.in_([f"oauth:{row.id}:access",
                                f"oauth:{row.id}:refresh"])))
    cfg = dict(row.config or {})
    oauth_cfg = dict(cfg.get("oauth") or {})
    oauth_cfg["connected"] = False
    oauth_cfg.pop("access_expires_at", None)
    cfg["oauth"] = oauth_cfg
    row.config = cfg
    await db.commit()
