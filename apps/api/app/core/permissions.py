"""公司内的权限模型：谁可以做什么。

## 为什么要有这个模块

全系统原先只有一种判定：`Company.owner_id == user.id`。它简单、够用，
但带来三个问题：

1. **危险操作没有名字**。"谁可以删掉整家公司（连同项目、员工、成果、日志）"，
   这个问题没有任何一处能回答——只能把全部 DELETE 端点逐个读一遍。
   于是"这一步有多危险"既无法审查，也无法向用户表达。
2. **不可扩展**。将来引入"管理员 / 成员 / 只读"时，需要改动散落在各处的
   `if c.owner_id != user.id`，而不是改一张表。
3. **不区分可逆与不可逆**。删一条记忆与删一家公司在代码里长得一样重。

## 提供什么

* `Capability`：给操作命名，按"影响面 + 是否可逆"分五级；
* `ROLE_MATRIX`：一张**角色 × 能力**矩阵，"谁可以做什么"只在这一处定义；
* `resolve_role()`：归属判定集中到一点，现在是"公司所有者"，将来是多成员角色；
* `ensure_capability()`：端点用它声明"我这一下有多重"，
  不通过时统一 404（不泄露"这家公司存在但你没权限"）。

## 诚实的边界

P5 起 `resolve_role` 会真实读取三处事实源：`users.is_superadmin`、
`companies.owner_id` / `company_members`、`department_members`。
但**界面尚未提供成员管理入口**，因此除"所有者 + 超管"之外的三种角色
（ADMIN / MEMBER / VIEWER）目前只能由直接写库或后续迭代产生；
矩阵里它们是已声明的语义，端点在收到这些角色时会按矩阵正常放行。

一次 `resolve_role` 最多 4 条查询（company / user / 成员 / 部门成员各一次），
且 user 与 company 通常已被 `get_current_user`、`assert_company_owned` 取过，
命中 Session identity map 后不再发 SQL——**不存在按成员数增长的 N+1**。
"""
from __future__ import annotations

from enum import Enum

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.company import Company, Department
from app.models.membership import CompanyMember, DepartmentMember
from app.models.user import User


class Capability(str, Enum):
    """操作能力，按影响面与可逆性分级（越往下越危险）。"""

    VIEW = "view"                # 只读：看数据
    OPERATE = "operate"          # 日常写：建项目、下任务、招员工
    MANAGE = "manage"            # 可逆的破坏：删部门、解除技能绑定
    DESTRUCTIVE = "destructive"  # 不可逆或影响面大：删公司、批量删除、离职交接、全局急停
    ADMIN = "admin"              # 公司级配置与所有权变更

    def __str__(self) -> str:
        return self.value


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

    def __str__(self) -> str:
        return self.value


# 「谁可以做什么」的唯一事实源。
ROLE_MATRIX: dict[Role, frozenset[Capability]] = {
    Role.OWNER: frozenset(Capability),
    Role.ADMIN: frozenset({Capability.VIEW, Capability.OPERATE,
                           Capability.MANAGE, Capability.DESTRUCTIVE}),
    Role.MEMBER: frozenset({Capability.VIEW, Capability.OPERATE}),
    Role.VIEWER: frozenset({Capability.VIEW}),
}

# 展示顺序：从最安全到最危险
_CAPABILITY_ORDER = (Capability.VIEW, Capability.OPERATE, Capability.MANAGE,
                     Capability.DESTRUCTIVE, Capability.ADMIN)


def capabilities_of(role: Role | None) -> frozenset[Capability]:
    if role is None:
        return frozenset()
    return ROLE_MATRIX.get(role, frozenset())


def role_has(role: Role | None, capability: Capability | str) -> bool:
    """该角色是否具备指定能力。能力名大小写不敏感。"""
    try:
        cap = Capability(str(capability).lower())
    except ValueError:
        return False
    return cap in capabilities_of(role)


# `company_members.role` → `Role`。刻意**不含** dept_head：部门负责人身份
# 只存在于 department_members，混进公司角色会让"公司角色 × 部门角色"
# 退化成单一维度（见 scope.build_ctx 的 head_dept_ids）。
_MEMBER_ROLE_MAP: dict[str, Role] = {
    "owner": Role.OWNER,
    "company_admin": Role.ADMIN,
    "member": Role.MEMBER,
    "viewer": Role.VIEWER,
}


async def _company_member(db: AsyncSession, company_id: str,
                          user_id: str) -> CompanyMember | None:
    """该用户在该公司的成员行（最多一条，UNIQUE(company_id, user_id)）。"""
    result = await db.execute(
        select(CompanyMember)
        .where(CompanyMember.company_id == company_id,
               CompanyMember.user_id == user_id)
        .limit(1))
    return result.scalars().first()


def _is_active_member(member: CompanyMember | None) -> bool:
    return member is not None and (member.status or "").lower() == "active"


async def _has_department_membership(db: AsyncSession, company_id: str,
                                     user_id: str) -> bool:
    """用户是否是该公司**任一部门**的成员。

    子查询限定在该公司内：部门成员行本身不带 company_id，
    不限定就会出现"在 A 公司有部门成员行 → 被 B 公司当成成员"的串号。
    """
    row = (await db.execute(
        select(DepartmentMember.id)
        .where(DepartmentMember.user_id == user_id,
               DepartmentMember.department_id.in_(
                   select(Department.id).where(Department.company_id == company_id)))
        .limit(1))).first()
    return row is not None


async def resolve_role(db: AsyncSession, user_id: str,
                       company_id: str | None) -> Role | None:
    """解析用户在某公司的角色。返回 None 表示"这家公司与他无关"。

    顺序（P5 契约，改顺序等于改权限，务必同步测试）：

    0. `companies.owner_id` 命中 → OWNER（所有权是事实源，优先于超管捷径）
    1. 平台超管且在该公司**没有生效成员身份** → ADMIN（可进入任意公司，但不是所有者）
    2. active 的 `company_members.role='owner'` → OWNER
    3. active 成员：company_admin → ADMIN、member → MEMBER、viewer → VIEWER
    4. 仅在部门成员表里出现过 → MEMBER（部门负责人身份不改变公司角色）
    5. 其余 → None

    为什么所有权要排在超管之前：首位安装者通常同时是超管与公司所有者。
    若先命中超管捷径，他会拿到 ADMIN——而 ADMIN 按矩阵没有 ADMIN 级能力
    （"管理员不该拥有所有权级能力"），于是公司级配置会被自己的超管身份拦住。
    只对"超管且确实是所有者"生效；非所有者的超管仍按 ADMIN。

    每个请求都会调用这里，因此查询次数必须与"成员数量"无关：
    company / user / 成员 / 部门成员各至多一次（前两者通常命中 identity map）。
    "超管是否仍是成员"按 **active** 判定而不是"有没有行"：
    否则一条 invited / suspended 的残留行会把超管锁在自己平台之外。
    """
    if not company_id or not user_id:
        return None
    company = await db.get(Company, company_id)
    if company is None:
        return None

    # 0) 所有权：公司表是事实源
    if company.owner_id == user_id:
        return Role.OWNER

    member = await _company_member(db, company_id, user_id)
    member_active = _is_active_member(member)

    # 1) 超管：不是该公司生效成员时，按 ADMIN 处理（不能借此拿到所有权）
    if not member_active and await _is_superadmin(db, user_id):
        return Role.ADMIN

    member_role = (member.role or "").lower() if member is not None else ""
    # 2) 成员表里的 owner 是公司表所有权的镜像
    if member_active and member_role == "owner":
        return Role.OWNER

    # 3) 生效成员角色
    if member_active:
        mapped = _MEMBER_ROLE_MAP.get(member_role)
        if mapped is not None:
            return mapped

    # 4) 只在部门成员表里（没有任何 company_members 行）：视为普通成员。
    #    company_members 行存在但非 active（invited/suspended）的成员
    #    不能凭部门残留行进入公司（语义是"只在部门表里"，实现此前漏了这个前提）。
    if member is None and await _has_department_membership(db, company_id, user_id):
        return Role.MEMBER

    # 5) 与该公司无关
    return None


async def _is_superadmin(db: AsyncSession, user_id: str) -> bool:
    """取用户的超管标记（get_current_user 已加载时命中 identity map，不再发 SQL）。"""
    user = await db.get(User, user_id)
    return bool(user is not None and user.is_superadmin)


async def ensure_capability(db: AsyncSession, user_id: str,
                            company_id: str | None,
                            capability: Capability | str) -> Role:
    """校验能力；不通过统一抛 404。

    用 404 而不是 403 是有意为之：403 等于告诉调用方
    "这个资源存在，只是你没权限"，在多租户系统里这本身就是信息泄露
    （可用于枚举他人的 ID）。全系统其余接口也都是 404，保持一致。
    """
    role = await resolve_role(db, user_id, company_id)
    if not role_has(role, capability):
        raise HTTPException(404, "Not found")
    return role


def require_company_scope(capability: Capability | str = Capability.VIEW):
    """生成 FastAPI 依赖：**路由级**租户门禁。

    与 `require_capability` 的区别：请求没带 `company_id` 时跳过而不是拒绝。
    因为同一个 router 里常混有"不按公司取数"的端点（如公开市场检索），
    逐端点挂容易漏，挂到 router 上则新加的端点默认被覆盖。

    带 `company_id` 却无归属 → 404（不泄露"公司存在但你没权限"）。
    """

    async def _dep(request: Request, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)) -> None:
        # 从请求里取：不能用 Query 声明，否则与带 /{company_id} 路径参数的
        # 端点冲突（FastAPI 会直接拒绝注册）。
        company_id = (request.path_params.get("company_id")
                      or request.query_params.get("company_id"))
        if company_id:
            await ensure_capability(db, user.id, company_id, capability)

    return _dep


def require_capability(capability: Capability, company_param: str = "cid"):
    """生成 FastAPI 依赖：要求当前用户在该公司拥有指定能力。

    适合"公司 id 就在路径或查询参数里"的端点，例如::

        @router.delete("/companies/{cid}", status_code=204,
                       dependencies=[Depends(require_capability(
                           Capability.DESTRUCTIVE, "cid"))])

    生成的依赖带 `__aieos_capability__` 标记，护栏测试据此核对
    "每个破坏性端点是否声明了自己的危险级别"。
    """

    async def _dep(request: Request, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)) -> None:
        company_id = (request.path_params.get(company_param)
                      or request.query_params.get(company_param))
        await ensure_capability(db, user.id, company_id, capability)

    _dep.__aieos_capability__ = str(capability)  # type: ignore[attr-defined]
    return _dep


def matrix() -> list[dict]:
    """把矩阵导出成可序列化结构，供前端据此禁用/隐藏操作。"""
    return [
        {"role": str(role),
         "capabilities": [str(c) for c in _CAPABILITY_ORDER
                          if c in ROLE_MATRIX.get(role, frozenset())]}
        for role in (Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER)
    ]
