"""公司 / 部门成员关系（P5 权限地基）。

## 为什么单独一张表而不是往 users 上加一列

一个人可以同时属于多家公司（顾问、外包、集团下的多个主体），
"他是哪家公司的什么角色"因此是**关系**而不是**属性**。
把角色写在 `users` 上，第二天要支持第二家公司就得改表结构 + 改全部查询。

## 与既有模型的边界

* `companies.owner_id` 仍然是唯一的所有权事实源（历史数据、清理与删除都依赖它）；
  `company_members.role='owner'` 是它的**镜像**，方便"列出我的公司"一类查询
  与未来所有权转移（转移时两处一起改，见 setup 流程）。
* 部门负责人（head）只由 `department_members.role_in_dept` 表达，
  **不进** `company_members.role`——`Role` 枚举刻意不新增 dept_head，
  否则"公司角色"与"部门角色"会混成一个维度，矩阵无法表达"他在这家公司是
  member、在那个部门是 head"。
* `role` / `status` 用 String 而不是 `Enum`：SQLite 上的 Enum 会落成 CHECK 约束，
  后续新增角色（如 `auditor`）需要重建表，与"只加可空列"的升级策略冲突。
"""
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class CompanyMember(TimestampMixin, Base):
    """用户在某家公司里的角色。"""

    __tablename__ = "company_members"
    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_company_member"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # owner | company_admin | member | viewer —— 刻意不接受 dept_head（见模块说明）
    role: Mapped[str] = mapped_column(String(20), default="member")
    # invited | active | suspended —— 只有 active 参与角色解析
    status: Mapped[str] = mapped_column(String(12), default="invited")
    # 该成员可创建的 Agent 数量上限；NULL = 跟随公司/部门默认
    agent_quota: Mapped[int | None] = mapped_column(Integer)
    invited_by: Mapped[str | None] = mapped_column(String(36))


class DepartmentMember(TimestampMixin, Base):
    """用户在某部门里的身份（含部门负责人）。"""

    __tablename__ = "department_members"
    __table_args__ = (
        UniqueConstraint("department_id", "user_id", name="uq_department_member"),
    )

    department_id: Mapped[str] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # head | deputy | member —— 只有 head 进入"可写本部门"的判定
    role_in_dept: Mapped[str] = mapped_column(String(12), default="member")
