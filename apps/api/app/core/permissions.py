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

**当前只有 OWNER 一种角色可达。** 系统还没有"成员"这一概念；
引入多成员需要把全系统的归属判定统一改走角色解析（一次宽面改造），
并配套邀请/移除成员的界面，属于产品决策而不是纯技术改动。

因此矩阵里 ADMIN / MEMBER / VIEWER 是**已声明的未来语义，不是当前功能**，
请勿据此对外承诺。本模块的价值在于：结构与命名先立起来，
将来加角色时只需让 `resolve_role` 多返回一种角色、`ROLE_MATRIX` 多一行，
**不需要再动任何一个端点**。
"""
from __future__ import annotations

from enum import Enum

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.company import Company


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


async def resolve_role(db: AsyncSession, user_id: str,
                       company_id: str | None) -> Role | None:
    """解析用户在某公司的角色。返回 None 表示"这家公司与他无关"。

    多成员协作尚未开放：将来在这里查成员表即可，端点与矩阵都不用改。
    """
    if not company_id:
        return None
    company = await db.get(Company, company_id)
    if company is None:
        return None
    if company.owner_id == user_id:
        return Role.OWNER
    return None


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
