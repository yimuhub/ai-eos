"""BYOK 密钥库 API（E8）。

* PUT    /byok/{name}         写入 / 轮换（MANAGE）
* GET    /byok?company_id=    列出名称与时间（VIEW，绝不含密文）
* DELETE /byok/{name}?company_id=  删除（MANAGE）
* POST   /byok/{name}/verify  探活（只回答 configured，不发起外部请求）

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


class CompanyIn(BaseModel):
    company_id: str


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
    data: CompanyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """只探活"是否已配置"，不调用任何外部服务，也不解密/回显。"""
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.VIEW)
    _validate_name(name)
    row = await _find(db, data.company_id, name)
    if row is None:
        raise HTTPException(404, "密钥不存在")
    return {"name": name, "configured": True}
