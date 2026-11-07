"""成员邀请链接接口（P1）。

* `POST   /companies/{cid}/invites`        生成一次性邀请链接（MANAGE）
* `GET    /companies/{cid}/invites`        列出待接受的邀请（MANAGE）
* `DELETE /companies/{cid}/invites/{iid}`  撤销（MANAGE）
* `GET    /invites/{token}`                公开预览：公司名、角色、是否还可用
* `POST   /invites/{token}/accept`         登录用户凭链接加入公司

## 为什么接受邀请要单独一条公开预览

被邀请人点开链接时通常还没注册。预览不要求登录，只回答"这条链接还能用吗、
加入的是哪家公司、给什么角色"，让对方先确认再决定登录/注册——否则
"先注册再看内容"会让来路不明感变成流失。

## 越权口径

与全系统一致：看不到的公司一律 404，不用 403 泄露存在性。
生成 `company_admin` 邀请与成员接口同口径——只有 OWNER 可以。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import Capability, Role, role_has
from app.core.scope import build_ctx
from app.core.timeutil import iso_utc
from app.db.session import get_db
from app.models.company import Company, Department
from app.models.member_invite import MemberInvite, utcnow
from app.models.membership import CompanyMember, DepartmentMember
from app.models.user import User
from app.services.event_system import log_event

router = APIRouter(tags=["invites"])

InviteRole = Literal["member", "viewer", "company_admin"]


class InviteCreateIn(BaseModel):
    role: InviteRole = "member"
    department_id: str | None = None
    expires_in_days: int = Field(default=7, ge=1, le=30)


async def _by_token(db: AsyncSession, token: str) -> MemberInvite | None:
    token = (token or "").strip()
    if not token:
        return None
    return (await db.execute(
        select(MemberInvite).where(
            MemberInvite.token_hash == MemberInvite.hash_token(token))
        .limit(1))).scalars().first()


async def _verify_manage(db: AsyncSession, user: User, company_id: str):
    """归属+权限校验：能管成员的人才看得到邀请；其他人一律 404。"""
    ctx = await build_ctx(db, user, company_id)
    if not role_has(ctx.role, Capability.MANAGE):
        raise HTTPException(404, "Not found")
    return ctx


async def _department_name(db: AsyncSession, department_id: str | None,
                           company_id: str) -> str | None:
    if not department_id:
        return None
    dept = await db.get(Department, department_id)
    if dept is None or dept.company_id != company_id:
        return None
    return dept.name


async def _invite_dict(db: AsyncSession, row: MemberInvite,
                       include_token: str | None = None) -> dict:
    state, reason = row.state()
    out = {
        "id": row.id,
        "role": row.role,
        "department_id": row.department_id,
        "department_name": await _department_name(
            db, row.department_id, row.company_id),
        "state": state,
        "reason": reason,
        "expires_at": iso_utc(row.expires_at),
        "created_at": iso_utc(row.created_at),
        "link_path": f"/join/{include_token}" if include_token else None,
    }
    return out


@router.post("/companies/{cid}/invites", status_code=201)
async def create_invite(cid: str, data: InviteCreateIn,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    ctx = await _verify_manage(db, user, cid)
    if data.role == "company_admin" and ctx.role is not Role.OWNER:
        raise HTTPException(404, "Not found")

    if data.department_id:
        dept = await db.get(Department, data.department_id)
        if dept is None or dept.company_id != cid:
            raise HTTPException(404, "Not found")

    raw, row = MemberInvite.new(
        company_id=cid, role=data.role, department_id=data.department_id,
        created_by=user.id, days=data.expires_in_days)
    db.add(row)
    await db.flush()
    await log_event(
        db, "member.invite_created", "member_invite", row.id, "user", user.id,
        {"company_id": cid, "role": row.role,
         "department_id": row.department_id},
        company_id=cid)
    await db.commit()
    await db.refresh(row)

    payload = await _invite_dict(db, row, include_token=raw)
    # 明文 token 只在这里出现一次；列表接口永远不回显。
    payload["token"] = raw
    return payload


@router.get("/companies/{cid}/invites")
async def list_invites(cid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _verify_manage(db, user, cid)
    rows = (await db.execute(
        select(MemberInvite).where(MemberInvite.company_id == cid)
        .order_by(MemberInvite.created_at.desc()).limit(100)
    )).scalars().all()
    return {"invites": [await _invite_dict(db, row) for row in rows]}


@router.delete("/companies/{cid}/invites/{iid}")
async def revoke_invite(cid: str, iid: str,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await _verify_manage(db, user, cid)
    row = (await db.execute(
        select(MemberInvite).where(MemberInvite.id == iid,
                                   MemberInvite.company_id == cid)
        .limit(1))).scalars().first()
    if row is None:
        raise HTTPException(404, "邀请不存在")
    if row.used_at is not None:
        raise HTTPException(409, "该邀请已被使用，无法撤销")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await log_event(
            db, "member.invite_revoked", "member_invite", row.id,
            "user", user.id, {"company_id": cid}, company_id=cid)
    await db.commit()
    await db.refresh(row)
    return await _invite_dict(db, row)


@router.get("/invites/{token}")
async def preview_invite(token: str, db: AsyncSession = Depends(get_db)):
    """公开预览：不要求登录，也**不返回公司 id 与任何成员信息**。"""
    row = await _by_token(db, token)
    if row is None:
        raise HTTPException(404, "邀请链接无效")
    state, reason = row.state()
    company = await db.get(Company, row.company_id)
    inviter = await db.get(User, row.created_by) if row.created_by else None
    return {
        "valid": state == "valid",
        "state": state,
        "reason": reason,
        "company_name": company.name if company else None,
        "role": row.role,
        "department_name": await _department_name(
            db, row.department_id, row.company_id),
        "invited_by": (inviter.display_name or inviter.username)
        if inviter else None,
        "expires_at": iso_utc(row.expires_at),
    }


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """凭链接加入公司。链接用过即失效；已是成员则明确告知，不再重复加。"""
    row = await _by_token(db, token)
    if row is None:
        raise HTTPException(404, "邀请链接无效")
    state, reason = row.state()
    if state != "valid":
        raise HTTPException(410, reason or "邀请链接已不可用")

    company = await db.get(Company, row.company_id)
    if company is None:
        raise HTTPException(404, "公司不存在")

    existing = (await db.execute(
        select(CompanyMember).where(
            CompanyMember.company_id == row.company_id,
            CompanyMember.user_id == user.id)
        .limit(1))).scalars().first()
    if existing is not None:
        if existing.status == "suspended":
            raise HTTPException(409, "你已被移出该公司，请联系管理员恢复")
        raise HTTPException(409, "你已经是该公司成员")

    member = CompanyMember(company_id=row.company_id, user_id=user.id,
                           role=row.role, status="active",
                           invited_by=row.created_by)
    db.add(member)
    if row.department_id:
        dept = await db.get(Department, row.department_id)
        if dept is not None and dept.company_id == row.company_id:
            already = (await db.execute(
                select(DepartmentMember.id).where(
                    DepartmentMember.department_id == row.department_id,
                    DepartmentMember.user_id == user.id)
                .limit(1))).first()
            if already is None:
                db.add(DepartmentMember(
                    department_id=row.department_id, user_id=user.id,
                    role_in_dept="member"))
    row.used_by = user.id
    row.used_at = utcnow()
    await log_event(
        db, "member.joined_by_invite", "member_invite", row.id,
        "user", user.id,
        {"company_id": row.company_id, "role": row.role,
         "department_id": row.department_id},
        company_id=row.company_id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "你已经是该公司成员")

    return {
        "company_id": company.id,
        "company_name": company.name,
        "role": row.role,
        "department_name": await _department_name(
            db, row.department_id, row.company_id),
    }
