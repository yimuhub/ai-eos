"""成员管理接口（P5）：把账号分配给人、指定部门负责人。

## 为什么单独一组路由

`company_members` / `department_members` 两张表在 P5 地基里已经就位，
但此前只有模型、没有入口——老板只能直接写库。这里补上最小闭环：

* `GET    /companies/{cid}/members`          按调用者角色裁剪的成员列表
* `POST   /companies/{cid}/members`          建账号（或复用已有邮箱）并加入公司
* `PATCH  /companies/{cid}/members/{mid}`    改公司角色 / 状态 / 配额 / 部门
* `DELETE /companies/{cid}/members/{mid}`    软停用（不物理删除）

## 两条容易写错的边界

1. **部门负责人不是公司角色**。`company_members.role` 只接受
   owner / company_admin / member / viewer；"某部门负责人"只由
   `department_members.role_in_dept='head'` 表达（见 models/membership.py）。
   因此 POST 的 `role` 里没有 dept_head，PATCH 改部门角色也只写
   `department_members`，绝不污染公司角色。
2. **越权一律 404**。与全系统口径一致：403 等于告诉调用方"资源存在但你没权限"。
   业务冲突（重复加入、已有负责人、停用 owner）才用 409。

## 软停用的实际语义

`DELETE` 只把 `company_members.status` 置为 `suspended`，不删行、不动
`users.is_active`（账号仍可登录、`/auth/me` 仍可查）。但 `resolve_role`
的最后一步是"只在部门成员表里出现过 → MEMBER"：如果只改状态而不清理
`department_members`，被停用的人仍会凭部门关系继续进入公司。因此停用时
同时移除该用户在本公司的部门成员行——否则"停用"只是界面上的一个标签。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import (AliasChoices, BaseModel, EmailStr, Field,
                      field_validator)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.auth import validate_password_strength
from app.core.permissions import Capability, Role, role_has
from app.core.scope import build_ctx
from app.core.security import hash_password
from app.core.timeutil import iso_utc
from app.db.session import get_db
from app.models.company import Company, Department
from app.models.membership import CompanyMember, DepartmentMember
from app.models.user import User
from app.services.event_system import log_event

router = APIRouter(tags=["members"])

DeptRole = Literal["member", "head"]
CompanyRole = Literal["member", "viewer", "company_admin"]


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class MemberCreateIn(BaseModel):
    email: EmailStr
    username: str | None = None
    password: str | None = None
    display_name: str | None = None
    role: CompanyRole = "member"
    department_ids: list[str] = Field(default_factory=list)
    department_role: DeptRole = "member"
    agent_quota: int | None = None
    # 目标部门已有负责人时，是否让新负责人顶替（旧的降为普通成员）
    replace_head: bool = False

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) < 2:
            raise ValueError("用户名至少 2 个字符")
        if len(v) > 50:
            raise ValueError("用户名最多 50 个字符")
        return v

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 100:
            raise ValueError("显示名称最多 100 个字符")
        return v

    @field_validator("department_ids")
    @classmethod
    def _clean_department_ids(cls, v: list[str]) -> list[str]:
        return _dedupe_ids(v)

    @field_validator("agent_quota")
    @classmethod
    def _check_quota(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("agent_quota 不能为负数")
        return v


class DepartmentAssignmentIn(BaseModel):
    department_id: str
    # 同时接受 role_in_dept / role / department_role，减少前端拼错字段的概率
    role_in_dept: DeptRole = Field(
        default="member",
        validation_alias=AliasChoices("role_in_dept", "role",
                                      "department_role"))

    @field_validator("department_id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("department_id 不能为空")
        return v


class MemberPatchIn(BaseModel):
    role: CompanyRole | None = None
    status: Literal["active", "suspended"] | None = None
    agent_quota: int | None = None
    # 传 department_ids 表示"用同一个部门角色整体替换"；
    # 传 departments 则支持每个部门不同角色（deputy 预留，本接口只收 member/head）。
    department_ids: list[str] | None = None
    department_role: DeptRole | None = None
    # 兼容三种写法：["id"]、[{"department_id": "id", "role_in_dept": "head"}]，
    # 都归一成 [(department_id, role_in_dept)]
    departments: list[DepartmentAssignmentIn | str] | None = None

    @field_validator("department_ids")
    @classmethod
    def _clean_department_ids(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _dedupe_ids(v)

    @field_validator("agent_quota")
    @classmethod
    def _check_quota(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("agent_quota 不能为负数")
        return v


def _dedupe_ids(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        v = (raw or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# 序列化与查询辅助
# ---------------------------------------------------------------------------
def _member_dict(member: CompanyMember, user: User,
                 departments: list[dict]) -> dict:
    """成员详情。**不含 password_hash**（响应体永远不碰凭据）。"""
    return {
        "id": member.id,
        "user_id": member.user_id,
        "email": user.email if user is not None else None,
        "username": user.username if user is not None else None,
        "display_name": user.display_name if user is not None else None,
        "role": member.role,
        "status": member.status,
        "agent_quota": member.agent_quota,
        "quota": member.agent_quota,   # 兼容前端/文档里的短名
        "invited_by": member.invited_by,
        "departments": departments,
        "department_ids": [d["department_id"] for d in departments],
        "created_at": iso_utc(member.created_at),
        "updated_at": iso_utc(member.updated_at),
    }


async def _department_map(db: AsyncSession, company_id: str,
                          user_ids: list[str],
                          only_dept_ids: frozenset[str] | None = None,
                          ) -> dict[str, list[dict]]:
    """一次查出这批用户在本公司的部门与部门角色。"""
    if not user_ids:
        return {}
    stmt = (
        select(DepartmentMember, Department)
        .join(Department, Department.id == DepartmentMember.department_id)
        .where(Department.company_id == company_id,
               DepartmentMember.user_id.in_(user_ids))
    )
    if only_dept_ids is not None:
        stmt = stmt.where(DepartmentMember.department_id.in_(only_dept_ids))
    rows = (await db.execute(stmt)).all()

    out: dict[str, list[dict]] = {}
    for dm, dept in rows:
        out.setdefault(dm.user_id, []).append({
            "id": dept.id,
            "department_id": dept.id,          # 兼容两种字段名
            "name": dept.name,
            "role_in_dept": dm.role_in_dept,
            "role": dm.role_in_dept,           # 同上
        })
    return out


async def _member_detail(db: AsyncSession, company_id: str, mid: str,
                         only_dept_ids: frozenset[str] | None = None) -> dict:
    row = (await db.execute(
        select(CompanyMember, User)
        .join(User, User.id == CompanyMember.user_id)
        .where(CompanyMember.company_id == company_id,
               CompanyMember.id == mid)
        .limit(1))).first()
    if row is None:
        raise HTTPException(404, "Not found")
    member, user = row
    depts = await _department_map(db, company_id, [member.user_id],
                                  only_dept_ids)
    return _member_dict(member, user, depts.get(member.user_id, []))


async def _load_company_departments(db: AsyncSession, company_id: str,
                                    department_ids: list[str],
                                    ) -> dict[str, Department]:
    """部门必须属于当前公司，任何一个不存在 → 404（不泄露部门归属）。"""
    ids = list(dict.fromkeys(department_ids))
    if not ids:
        return {}
    rows = (await db.execute(
        select(Department).where(Department.id.in_(ids),
                                 Department.company_id == company_id)
    )).scalars().all()
    found = {d.id: d for d in rows}
    if len(found) != len(ids):
        raise HTTPException(404, "Not found")
    return found


async def _member_in_departments(db: AsyncSession, user_id: str,
                                 department_ids: frozenset[str]) -> bool:
    if not department_ids:
        return False
    row = (await db.execute(
        select(DepartmentMember.id).where(
            DepartmentMember.user_id == user_id,
            DepartmentMember.department_id.in_(department_ids))
        .limit(1))).first()
    return row is not None


async def _heads_of_departments(db: AsyncSession,
                                department_ids: list[str],
                                exclude_user_id: str | None = None,
                                ) -> list[DepartmentMember]:
    """这些部门里现有的"真人"负责人（部门成员行）。"""
    if not department_ids:
        return []
    stmt = select(DepartmentMember).where(
        DepartmentMember.department_id.in_(department_ids),
        DepartmentMember.role_in_dept == "head")
    if exclude_user_id:
        stmt = stmt.where(DepartmentMember.user_id != exclude_user_id)
    return list((await db.execute(stmt)).scalars().all())


async def _upsert_departments(db: AsyncSession, user_id: str,
                              assignments: list[tuple[str, str]]) -> None:
    """POST 用：只补/改指定部门，不动该用户其它部门关系。"""
    if not assignments:
        return
    dept_ids = [did for did, _ in assignments]
    existing = {
        row.department_id: row
        for row in (await db.execute(
            select(DepartmentMember).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id.in_(dept_ids))
        )).scalars().all()
    }
    for did, role in assignments:
        row = existing.get(did)
        if row is not None:
            row.role_in_dept = role
        else:
            db.add(DepartmentMember(department_id=did, user_id=user_id,
                                    role_in_dept=role))
    await db.flush()


async def _clear_company_departments(db: AsyncSession, company_id: str,
                                     user_id: str) -> list[str]:
    """移除该用户在本公司的全部部门成员行，返回被移除的部门 id。"""
    dept_id_subq = select(Department.id).where(
        Department.company_id == company_id)
    ids = list((await db.execute(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user_id,
            DepartmentMember.department_id.in_(dept_id_subq))
    )).scalars().all())
    if ids:
        await db.execute(
            delete(DepartmentMember).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id.in_(ids)))
        await db.flush()
    return ids


async def _ensure_owner_membership(db: AsyncSession, company: Company) -> None:
    """老库的自愈：owner 可能没有 company_members 镜像行（P5 之前建的公司）。

    只在缺失时补一行，幂等；不补的话 owner 自己在成员列表里会"消失"，
    而 `resolve_role` / `accessible_company_ids` 早已兼容两种事实源。
    """
    if company is None or not company.owner_id:
        return
    row = (await db.execute(
        select(CompanyMember.id).where(
            CompanyMember.company_id == company.id,
            CompanyMember.user_id == company.owner_id)
        .limit(1))).first()
    if row is not None:
        return
    db.add(CompanyMember(company_id=company.id, user_id=company.owner_id,
                         role="owner", status="active"))
    try:
        await db.commit()
    except IntegrityError:
        # 并发请求同时补行：唯一约束兜底，对方已经补上了
        await db.rollback()


# ---------------------------------------------------------------------------
# GET：按角色裁剪的列表
# ---------------------------------------------------------------------------
@router.get("/companies/{cid}/members")
async def list_members(cid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """owner / company_admin 看全公司；部门负责人只看自己负责的部门；
    普通成员（含 viewer）只看自己那一条；与公司无关 → 404。"""
    ctx = await build_ctx(db, user, cid)
    company = await db.get(Company, cid)
    await _ensure_owner_membership(db, company)

    stmt = (
        select(CompanyMember, User)
        .join(User, User.id == CompanyMember.user_id)
        .where(CompanyMember.company_id == cid)
    )
    trim_to: frozenset[str] | None = None
    if not ctx.sees_all_agents:
        if ctx.head_dept_ids:
            # 只返回在自己负责部门里有部门成员行的人
            stmt = stmt.where(CompanyMember.user_id.in_(
                select(DepartmentMember.user_id).where(
                    DepartmentMember.department_id.in_(ctx.head_dept_ids))))
            # 同一个人的部门列表也裁剪到这些部门
            trim_to = ctx.head_dept_ids
        else:
            stmt = stmt.where(CompanyMember.user_id == user.id)

    rows = (await db.execute(
        stmt.order_by(CompanyMember.created_at, CompanyMember.id))).all()
    dept_map = await _department_map(
        db, cid, [m.user_id for m, _ in rows], trim_to)
    return [_member_dict(m, u, dept_map.get(m.user_id, [])) for m, u in rows]


# ---------------------------------------------------------------------------
# POST：建账号 / 复用账号并加入公司
# ---------------------------------------------------------------------------
@router.post("/companies/{cid}/members", status_code=201)
async def add_member(cid: str, data: MemberCreateIn,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """把一个人加进公司。

    * 管理公司成员是 MANAGE；授予 `company_admin` 只有 OWNER 可以。
    * 邮箱已存在 → 复用该 User（**不重置密码**）；不存在 → 必须带 username+password。
    * 同一公司重复加入 → 409；部门不属于本公司 → 404。
    * `department_role=head` 且部门已有负责人 → 409，除非 `replace_head=true`。
    """
    ctx = await build_ctx(db, user, cid)
    if not role_has(ctx.role, Capability.MANAGE):
        raise HTTPException(404, "Not found")
    if data.role == "company_admin" and ctx.role is not Role.OWNER:
        raise HTTPException(404, "Not found")
    # 老库 owner 可能没有成员镜像行：先补齐，重复加入判定才认得 owner
    await _ensure_owner_membership(db, await db.get(Company, cid))

    # 1) 部门先校验（任何写库之前）：不属于本公司一律 404
    await _load_company_departments(db, cid, data.department_ids)

    # 2) 负责人冲突先判断（同样在任何写库之前）
    head_assignments = (data.department_ids
                        if data.department_role == "head" else [])
    existing_heads = await _heads_of_departments(db, head_assignments)
    if existing_heads and not data.replace_head:
        raise HTTPException(409, "该部门已有负责人")

    email = str(data.email)
    email_l = email.lower()
    existing_user = (await db.execute(
        select(User).where(func.lower(User.email) == email_l).limit(1)
    )).scalars().first()

    try:
        if existing_user is None:
            # 新账号：用户名与密码都是必填，缺失/太弱 → 422
            if not data.username or not data.password:
                raise HTTPException(422, "新成员必须提供 username 和 password")
            try:
                validate_password_strength(data.password)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

            taken = (await db.execute(
                select(User.id).where(
                    func.lower(User.username) == data.username.lower())
                .limit(1))).first()
            if taken is not None:
                raise HTTPException(409, "该用户名已被注册")

            target_user = User(
                email=email,
                username=data.username,
                password_hash=hash_password(data.password),
                display_name=data.display_name or data.username,
                is_active=True,
            )
            db.add(target_user)
            await db.flush()
        else:
            # 邮箱已存在：复用账号，绝不改密码 / 昵称
            target_user = existing_user

        duplicate = (await db.execute(
            select(CompanyMember.id).where(
                CompanyMember.company_id == cid,
                CompanyMember.user_id == target_user.id)
            .limit(1))).first()
        if duplicate is not None:
            raise HTTPException(409, "该用户已是公司成员")

        member = CompanyMember(
            company_id=cid,
            user_id=target_user.id,
            role=data.role,
            status="active",
            agent_quota=data.agent_quota,
            invited_by=user.id,
        )
        db.add(member)
        await db.flush()

        if data.replace_head:
            for row in existing_heads:
                if row.user_id != target_user.id:
                    row.role_in_dept = "member"
        await _upsert_departments(
            db, target_user.id,
            [(did, data.department_role) for did in data.department_ids])

        await log_event(
            db, "member.added", "company_member", member.id, "user", user.id,
            {"company_id": cid, "user_id": target_user.id,
             "email": target_user.email, "role": member.role,
             "department_ids": data.department_ids,
             "department_role": data.department_role,
             "reused_account": existing_user is not None},
            company_id=cid,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError:
        # 并发下邮箱/用户名/成员唯一约束是最后兜底
        await db.rollback()
        raise HTTPException(409, "该用户或邮箱已存在，请刷新后重试")
    except Exception:
        await db.rollback()
        raise

    return await _member_detail(db, cid, member.id)


# ---------------------------------------------------------------------------
# PATCH：改公司角色 / 状态 / 配额 / 部门（整体替换）
# ---------------------------------------------------------------------------
def _resolve_assignments(data: MemberPatchIn,
                         current_dept_ids: list[str],
                         ) -> list[tuple[str, str]] | None:
    """把三种部门写法归一成 [(dept_id, role)]；None 表示"本次不改部门"。"""
    if data.departments is not None and data.department_ids is not None:
        raise HTTPException(422, "departments 与 department_ids 不能同时提供")
    if data.departments is not None:
        merged: dict[str, str] = {}
        for item in data.departments:
            if isinstance(item, str):
                merged[item.strip()] = data.department_role or "member"
            else:
                merged[item.department_id] = item.role_in_dept
        return list(merged.items())
    if data.department_ids is not None:
        role = data.department_role or "member"
        return [(did, role) for did in data.department_ids]
    if data.department_role is not None:
        # 只给角色：作用到该成员当前的全部部门
        return [(did, data.department_role) for did in current_dept_ids]
    return None


@router.patch("/companies/{cid}/members/{mid}")
async def update_member(cid: str, mid: str, data: MemberPatchIn,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """MANAGE 才能改；部门负责人只能动自己部门内的人。

    * 只有 OWNER 能授予/撤销 `company_admin`（管理员也不能动同级管理员）。
    * 部门负责人（即便公司角色只是 member）可以在**自己负责的部门**内
      改成员的部门归属；部门整体替换只允许落到自己负责的部门，因此他们
      无法把别人设成别的部门的 head。
    * 停用与配额属于公司级处置权（MANAGE），部门负责人不能改——
      停用会切断全部访问且负责人自己无法恢复，配额会拦住别人建 Agent。
    * 停用（status=suspended）同时清掉部门成员行，保证权限真的失效。
    """
    ctx = await build_ctx(db, user, cid)
    full_manage = role_has(ctx.role, Capability.MANAGE)
    if not full_manage and not ctx.head_dept_ids:
        raise HTTPException(404, "Not found")

    row = (await db.execute(
        select(CompanyMember).where(CompanyMember.id == mid,
                                    CompanyMember.company_id == cid)
        .limit(1))).scalars().first()
    if row is None:
        raise HTTPException(404, "Not found")
    member = row
    company = await db.get(Company, cid)
    target_user = await db.get(User, member.user_id)
    if target_user is None:
        raise HTTPException(404, "Not found")

    current_role = (member.role or "").lower()
    is_owner_target = (company is not None
                       and company.owner_id == member.user_id) \
        or current_role == "owner"

    # 部门负责人（无 MANAGE）的作用域：目标必须在自己负责的部门里
    if not full_manage:
        if current_role in ("owner", "company_admin"):
            # 不能动比自己级别高的成员（否则可越权停用管理员）
            raise HTTPException(404, "Not found")
        if data.role is not None and data.role != member.role:
            # 公司角色是公司级事实，部门负责人只能改部门角色
            raise HTTPException(404, "Not found")
        if (data.status is not None and data.status != member.status) \
                or "agent_quota" in data.model_fields_set:
            # 停用会切断全部公司访问且负责人无法恢复；配额决定别人能否
            # 建 Agent——两者都是公司级处置权（MANAGE）。
            raise HTTPException(404, "Not found")
        if not await _member_in_departments(db, member.user_id,
                                            ctx.head_dept_ids):
            raise HTTPException(404, "Not found")

    # OWNER 自身不可被改角色/停用（所有权只通过转移流程变更）
    if is_owner_target:
        if data.role is not None and data.role != member.role:
            raise HTTPException(409, "不能修改所有者的角色")
        if data.status == "suspended" and member.status != "suspended":
            raise HTTPException(409, "不能停用公司所有者")

    # 只有 OWNER 能改出/改掉 company_admin（含管理员之间的互相降级）
    changing_role = data.role is not None and data.role != member.role
    changing_status = data.status is not None and data.status != member.status
    if ctx.role is not Role.OWNER and changing_role:
        if data.role == "company_admin" or current_role == "company_admin":
            raise HTTPException(404, "Not found")

    # 自己不能停用自己（避免把自己锁在门外）
    if member.user_id == user.id and data.status == "suspended" \
            and member.status != "suspended":
        raise HTTPException(409, "不能停用自己的账号")

    current_depts = list((await db.execute(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == member.user_id,
            DepartmentMember.department_id.in_(
                select(Department.id).where(Department.company_id == cid)))
    )).scalars().all())
    assignments = _resolve_assignments(data, current_depts)

    if assignments is not None:
        await _load_company_departments(
            db, cid, [did for did, _ in assignments])
        if not full_manage:
            outside = [did for did, _ in assignments
                       if did not in ctx.head_dept_ids]
            if outside:
                # 想把别人挂到自己不负责的部门（包括设成那里的 head）→ 404
                raise HTTPException(404, "Not found")

    changes: dict = {}
    try:
        if changing_role:
            member.role = data.role
            changes["role"] = data.role
        if changing_status:
            member.status = data.status
            changes["status"] = data.status
        if "agent_quota" in data.model_fields_set:
            member.agent_quota = data.agent_quota
            changes["agent_quota"] = data.agent_quota

        if assignments is not None:
            dept_id_subq = select(Department.id).where(
                Department.company_id == cid)
            if not full_manage:
                # 负责人只能替换自己负责部门内的关系；别人在其它部门的
                # 成员行不归他管（否则"替换成 [A]"会顺手把人从 B 踢掉）。
                dept_id_subq = dept_id_subq.where(
                    Department.id.in_(ctx.head_dept_ids))
            await db.execute(
                delete(DepartmentMember).where(
                    DepartmentMember.user_id == member.user_id,
                    DepartmentMember.department_id.in_(dept_id_subq)))
            for did, role in assignments:
                db.add(DepartmentMember(department_id=did,
                                        user_id=member.user_id,
                                        role_in_dept=role))
            changes["departments"] = [{"department_id": did,
                                       "role_in_dept": role}
                                      for did, role in assignments]

        # 停用必须真的断权：resolve_role 会把"只在部门成员表里的人"当 MEMBER
        if (member.status or "").lower() == "suspended":
            removed = await _clear_company_departments(db, cid, member.user_id)
            if removed:
                changes["removed_department_ids"] = removed

        await db.flush()
        if changes:
            await log_event(
                db, "member.updated", "company_member", member.id,
                "user", user.id,
                {"company_id": cid, "user_id": member.user_id, **changes},
                company_id=cid,
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "成员更新冲突，请刷新后重试")
    except Exception:
        await db.rollback()
        raise

    # 负责人看到的部门列表同样裁剪到他自己负责的部门（与 GET 一致）
    return await _member_detail(
        db, cid, member.id,
        None if full_manage else ctx.head_dept_ids)


# ---------------------------------------------------------------------------
# DELETE：软停用
# ---------------------------------------------------------------------------
@router.delete("/companies/{cid}/members/{mid}", status_code=204)
async def suspend_member(cid: str, mid: str,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """软停用：status=suspended，不物理删除；不能停用 owner，也不能停用自己。"""
    ctx = await build_ctx(db, user, cid)
    if not role_has(ctx.role, Capability.MANAGE):
        raise HTTPException(404, "Not found")

    member = (await db.execute(
        select(CompanyMember).where(CompanyMember.id == mid,
                                    CompanyMember.company_id == cid)
        .limit(1))).scalars().first()
    if member is None:
        raise HTTPException(404, "Not found")

    company = await db.get(Company, cid)
    current_role = (member.role or "").lower()
    if (company is not None and company.owner_id == member.user_id) \
            or current_role == "owner":
        raise HTTPException(409, "不能停用公司所有者")
    if member.user_id == user.id:
        raise HTTPException(409, "不能停用自己的账号")

    if (member.status or "").lower() == "suspended":
        return  # 幂等：已停用不再重复写事件

    try:
        member.status = "suspended"
        removed = await _clear_company_departments(db, cid, member.user_id)
        await log_event(
            db, "member.suspended", "company_member", member.id,
            "user", user.id,
            {"company_id": cid, "user_id": member.user_id,
             "removed_department_ids": removed},
            company_id=cid,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise