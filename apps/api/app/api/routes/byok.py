"""BYOK 密钥库 API（E8）。

* PUT    /byok/{name}         写入 / 轮换（MANAGE）
* GET    /byok?company_id=    列出名称与时间（VIEW，绝不含密文）
* DELETE /byok/{name}?company_id=  删除（MANAGE）
* POST   /byok/{name}/verify  探活（只报 configured；带 provider 时真实探测）

密钥解析顺序（运行时）：公司密钥库 → 环境变量。见
`app/services/api_key_resolver.py`——客户填的 Key 必须真的被网关用上。

明文只在 PUT 请求体内出现一次，写入前立即加密；任何响应与日志都不含
明文或密文。名称限制为 `[A-Za-z0-9_.-]{1,80}`，值限制 1–4096 字符，
非法输入统一 400（而不是 422，便于前端展示同一条错误）。
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.company_secret import CompanySecret
from app.models.user import User
from app.schemas.responses import (
    ByokDeletedOut,
    ByokSecretOut,
    ByokUpsertOut,
    ByokVerifyOut,
)
from app.services import secret_vault

router = APIRouter(prefix="/byok", tags=["byok"])

NAME_PATTERN = r"[A-Za-z0-9_.-]{1,80}"
_NAME_RE = re.compile(rf"^{NAME_PATTERN}$")
MAX_VALUE_LENGTH = 4096


class SecretIn(BaseModel):
    company_id: str
    value: str


class ByokVerifyIn(BaseModel):
    """`provider` 缺省时只报"已配置"；给了才做真实探测。

    `base_url` 只对 openai_compat 类自建网关有意义（官方/中转站地址不同），
    探测路径固定为 `/models`，不接受任意路径。
    """

    company_id: str
    provider: str | None = None
    base_url: str | None = None


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name or ""):
        raise HTTPException(400, "name 必须匹配 [A-Za-z0-9_.-]{1,80}")


def _validate_value(value: str) -> None:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_VALUE_LENGTH):
        raise HTTPException(400, "value 长度必须为 1-4096 字符")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _find(db: AsyncSession, company_id: str,
                name: str) -> CompanySecret | None:
    return (await db.execute(
        select(CompanySecret)
        .where(CompanySecret.company_id == company_id,
               CompanySecret.name == name)
        .limit(1))).scalars().first()


@router.put("/{name}", response_model=ByokUpsertOut)
async def put_secret(
    name: str,
    data: SecretIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    _validate_name(name)
    _validate_value(data.value)

    row = await _find(db, data.company_id, name)
    rotated = row is not None
    ciphertext = secret_vault.encrypt(data.value)
    if row is None:
        row = CompanySecret(company_id=data.company_id, name=name,
                            ciphertext=ciphertext)
        db.add(row)
    else:
        row.ciphertext = ciphertext
    # flush + refresh：让 created_at / updated_at 拿到数据库侧的值，
    # 响应里返回真实时间而不是 None。
    await db.flush()
    await db.refresh(row)
    await db.commit()
    return {"name": row.name, "updated_at": _iso(row.updated_at),
            "rotated": rotated}


@router.get("", response_model=list[ByokSecretOut])
async def list_secrets(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = (await db.execute(
        select(CompanySecret)
        .where(CompanySecret.company_id == company_id)
        .order_by(CompanySecret.name))).scalars().all()
    # 只暴露元数据：name / created_at / updated_at。
    return [{"name": r.name, "created_at": _iso(r.created_at),
             "updated_at": _iso(r.updated_at)} for r in rows]


@router.delete("/{name}", response_model=ByokDeletedOut)
async def delete_secret(
    name: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    _validate_name(name)
    row = await _find(db, company_id, name)
    if row is None:
        raise HTTPException(404, "密钥不存在")
    await db.delete(row)
    await db.commit()
    return {"deleted": name}


@router.post("/{name}/verify", response_model=ByokVerifyOut)
async def verify_secret(
    name: str,
    data: ByokVerifyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """探活。

    * 只给 `company_id`：保持原语义——只回答"是否已配置"，不联网。
    * 再给 `provider`（可选 `base_url`）：**真实探测**——解密后向上游发一次
      只读的模型列表请求，回答"这把钥匙能不能用"。客户看到"已配置"之后
      最关心的是这个，只报 configured 等于让他到任务失败时才发现钥匙是坏的。

    任何情况下都不回显密钥；`detail` 只含状态码/主机名，不含凭据。
    """
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.VIEW)
    _validate_name(name)
    row = await _find(db, data.company_id, name)
    if row is None:
        raise HTTPException(404, "密钥不存在")
    if not data.provider:
        return {"name": name, "configured": True}

    try:
        api_key = secret_vault.decrypt(row.ciphertext)
    except Exception as exc:  # noqa: BLE001 — 解不开要给可操作原因，不能 500
        return {"name": name, "configured": True, "probed": True,
                "valid": False, "reachable": None,
                "detail": f"密钥无法解密（{type(exc).__name__}）："
                          "可能更换过服务端签名密钥，请重新录入该密钥。"}

    probe = await _probe_provider_key(
        data.provider, data.base_url, api_key)
    return {"name": name, "configured": True, "probed": True, **probe}


# 探测端点：只用只读的"列模型"接口，不发起推理，不消耗 token。
_PROBE_TARGETS = {
    "openai_compat": ("https://api.openai.com/v1", "/models"),
    "anthropic": ("https://api.anthropic.com", "/v1/models"),
    "gemini": ("https://generativelanguage.googleapis.com", "/v1beta/models"),
}


def _probe_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        # 放请求头而不是 ?key=：查询串会进代理/访问日志，凭据不该留在那里。
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


async def _probe_provider_key(provider: str, base_url: str | None,
                              api_key: str) -> dict:
    """向上游发一次只读请求，判断密钥是否可用。

    网络不可达与密钥被拒必须分开：前者是部署/网络问题，逼客户换 Key
    只会浪费他的时间。
    """
    import httpx

    from app.core.http_client import http_client
    from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async

    if provider not in _PROBE_TARGETS:
        raise HTTPException(
            400, f"provider 只能是 {sorted(_PROBE_TARGETS)}")
    default_base, path = _PROBE_TARGETS[provider]
    root = (base_url or default_base).strip().rstrip("/")
    url = f"{root}{path}"
    try:
        await validate_outbound_url_async(url, allow_private=True)
    except OutboundURLRejected as e:
        raise HTTPException(400, f"base_url 不安全：{e}")

    try:
        async with http_client(timeout=httpx.Timeout(8.0)) as client:
            resp = await client.get(url, headers=_probe_headers(provider, api_key))
    except httpx.HTTPError as e:
        return {"valid": None, "reachable": False, "status_code": None,
                "detail": f"无法连接上游（{type(e).__name__}）："
                          "请检查网络、代理或 base_url。"}

    status = resp.status_code
    if 200 <= status < 300:
        return {"valid": True, "reachable": True, "status_code": status,
                "detail": "密钥可用。"}
    if status in (401, 403):
        return {"valid": False, "reachable": True, "status_code": status,
                "detail": ("密钥被服务商拒绝（HTTP %d）：可能已失效、"
                           "与站点不匹配或缺少权限。" % status)}
    if status == 429:
        return {"valid": None, "reachable": True, "status_code": status,
                "detail": "被上游限流（429），无法判断密钥是否有效，稍后重试。"}
    return {"valid": None, "reachable": True, "status_code": status,
            "detail": (f"上游返回 HTTP {status}，无法判断密钥是否有效："
                       "请检查 base_url 是否指向兼容接口。")}
