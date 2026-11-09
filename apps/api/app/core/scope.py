"""Agent / 部门级别的**行级**可见与可写范围（P5）。

## 为什么需要它

`resolve_role` 只回答"他在公司里是什么角色"，回答不了
"他在这家公司里能看到哪几个数字员工"。可见性是**行级**问题：
同一个 MEMBER，看得见本部门的同事、自己建的私有 Agent、
以及全公司公开的那部分；看不见别的部门的私有 Agent。

把这段判定写在各路由里，一定会漂移（有人写 `department_id == user.dept`、
有人忘了 `visibility != 'private'`）。因此这里只暴露"条件构造器"：

    stmt = select(Agent).where(agent_read_scope(ctx))

条件直接拼进 `select(...).where(...)`，**同时**用于列表过滤与单行校验
（`ensure_agent_access` 就是拿同一条件去查这一行）。这样"列表里看不到"
与"详情打不开"天然一致，不会出现"列表隐藏但详情 200"的经典漏洞。

## 两条边界

* 返回的条件**永远包含 `company_id`**。可见性再宽也不能跨公司，
  否则 owner 的"全公司"就会被拼进别的公司的行。
* 写范围比读范围窄：读可以看全公司公开的 Agent，写只能动自己
  （部门负责人额外能动本部门的）。"能看"从不隐含"能改"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Capability, Role, resolve_role, role_has
from app.models.agent import Agent
from app.models.company import Company, Department
from app.models.membership import CompanyMember, DepartmentMember

# visibility 的三种取值；NULL 等价 company（升级前的老 Agent 全员可见）
VISIBILITY_COMPANY = "company"
VISIBILITY_DEPARTMENT = "department"
VISIBILITY_PRIVATE = "private"


def accessible_company_ids(user_id: str):
    """调用者可访问的公司 id 子查询：owner 事实源 ∪ active 成员行。

    tasks/skills/events/tokens/projects/governance 都曾在各自文件里抄一份
    同样的查询；口径一旦漂移（有人漏掉 active、有人漏掉 owner），
    "不传 company_id 就聚合全平台"的缺口会从某一条路由重新长回来。
    """
    owned = select(Company.id).where(Company.owner_id == user_id)
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user_id,
        CompanyMember.status == "active")
    return select(Company.id).where(
        or_(Company.id.in_(owned), Company.id.in_(member)))


@dataclass(frozen=True)
class CompanyCtx:
    """一次请求内"我是谁、我在哪家公司、我能看到什么"的不可变快照。

    只在 `build_ctx` 里构造：它的字段全部来自数据库，
    手工 new 一个会让调用方绕过角色解析（拿不到的是权限，不是语法糖）。
    """

    user_id: str
    company_id: str
    role: Role
    is_superadmin: bool = False
    dept_ids: frozenset[str] = field(default_factory=frozenset)       # 所在部门
    head_dept_ids: frozenset[str] = field(default_factory=frozenset)  # 负责的部门
    domains: frozenset[str] = field(default_factory=frozenset)        # 预留：data_domain_code

    @property
    def sees_all_agents(self) -> bool:
        """是否在整个公司范围内可见（owner / admin / 平台超管）。"""
        return self.is_superadmin or self.role in (Role.OWNER, Role.ADMIN)


async def build_ctx(db: AsyncSession, user, company_id: str) -> CompanyCtx:
    """构造请求上下文。与该公司无关（或无角色）→ 404。

    查询次数固定为 2 + resolve_role 的至多 4 条，与部门/成员数量无关。
    部门成员只查一次，顺带取回所属部门的 `data_domain_code`（预留数据域）。
    """
    if not company_id:
        raise HTTPException(404, "Not found")
    role = await resolve_role(db, getattr(user, "id", None), company_id)
    if role is None:
        raise HTTPException(404, "Not found")

    rows = (await db.execute(
        select(DepartmentMember.department_id,
               DepartmentMember.role_in_dept,
               Department.data_domain_code)
        .join(Department, Department.id == DepartmentMember.department_id)
        .where(DepartmentMember.user_id == user.id,
               Department.company_id == company_id))).all()

    dept_ids = {r[0] for r in rows if r[0]}
    head_ids = {r[0] for r in rows if r[0] and (r[1] or "").lower() == "head"}
    domains = {r[2] for r in rows if r[2]}

    return CompanyCtx(
        user_id=user.id,
        company_id=company_id,
        role=role,
        is_superadmin=bool(getattr(user, "is_superadmin", False)),
        dept_ids=frozenset(dept_ids),
        head_dept_ids=frozenset(head_ids),
        domains=frozenset(domains),
    )


def agent_read_scope(ctx: CompanyCtx):
    """列表/详情共用的可见条件。

    owner / admin / 超管 → 全公司；
    其余 → 本部门 ∪ 自己创建的 ∪ 公司公开（visibility='company' 或 NULL）。
    """
    base = Agent.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    return and_(
        base,
        or_(
            Agent.department_id.in_(ctx.dept_ids),
            Agent.created_by_user_id == ctx.user_id,
            Agent.visibility == VISIBILITY_COMPANY,
            Agent.visibility.is_(None),  # NULL 等价 company（历史数据）
        ),
    )


def agent_write_scope(ctx: CompanyCtx):
    """写操作的允许条件（含删除、改配置、跑任务）。

    owner / admin / 超管 → 全公司；
    部门负责人 → 本部门 ∪ 自己创建的；
    普通成员 / 只读 → 仅自己创建的。

    "部门负责人额外含 `created_by_user_id`" 是对契约措辞的必要补全：
    负责人若建了一个不挂部门的私有 Agent，只给"本部门"会让他
    建完就改不动自己的东西（写范围反而小于读范围），属于硬伤。
    这仍然**不**放宽到别人的私有 Agent。
    """
    base = Agent.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    branches = [Agent.created_by_user_id == ctx.user_id]
    if ctx.head_dept_ids:
        branches.append(Agent.department_id.in_(ctx.head_dept_ids))
    return and_(base, or_(*branches))


async def ensure_agent_access(db: AsyncSession, user, agent,
                              capability: Capability | str) -> None:
    """单行校验：角色不够或这一行不在范围内 → 404（不泄露存在性）。

    与列表用**同一套条件**：`select(Agent.id).where(id==..., scope)` 有行即可见。
    VIEW 走读范围，其余（OPERATE/MANAGE/DESTRUCTIVE/ADMIN）走写范围，
    因此"看得见但改不动"是常态，反之不可能。
    """
    if agent is None:
        raise HTTPException(404, "Not found")
    ctx = await build_ctx(db, user, agent.company_id)
    if not role_has(ctx.role, capability):
        raise HTTPException(404, "Not found")
    cap = Capability(str(capability).lower())
    scope = agent_read_scope(ctx) if cap is Capability.VIEW else agent_write_scope(ctx)
    found = (await db.execute(
        select(Agent.id).where(Agent.id == agent.id, scope).limit(1))).first()
    if found is None:
        raise HTTPException(404, "Not found")


async def ensure_dept_access(ctx: CompanyCtx, department_id: str, *,
                             db: AsyncSession | None = None) -> None:
    """部门级门禁：不属于可见范围 → 404。

    普通成员/负责人的判定完全落在 ctx 里（dept_ids 已按公司过滤，跨公司 id 必然不命中）。
    owner/admin/超管天然全公司可见，此时若有 `db` 就再确认一次"这个部门确实属于
    当前公司"，防止调用方拿着别的公司的部门 id 通过门禁（可选参数，不传时
    由调用方保证 department_id 已按 company_id 取过）。
    """
    if not department_id:
        raise HTTPException(404, "Not found")
    if ctx.sees_all_agents:
        if db is not None:
            row = (await db.execute(
                select(Department.id).where(Department.id == department_id,
                                            Department.company_id == ctx.company_id)
                .limit(1))).first()
            if row is None:
                raise HTTPException(404, "Not found")
        return
    if department_id in ctx.dept_ids or department_id in ctx.head_dept_ids:
        return
    raise HTTPException(404, "Not found")
