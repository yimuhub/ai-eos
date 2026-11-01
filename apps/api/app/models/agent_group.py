"""部门下的「组」：CEO 按需招人时的落位层级。

## 为什么在部门之下再分一层

部门回答「这条业务归谁管」，组回答「这几个人现在一起干哪件事」。
CEO 拆解目标时经常需要「内容组」「投放组」这种临时小队，又不该为每个
小队新建一个部门（部门是长期架构，组是可增删的作战单元）。

## 约束

* 组必须属于某个公司；`department_id` 可空是为了兼容"公司直属组"，
  但 CEO 自动建人时一定先解析/创建部门，再在部门下建组。
* 同部门下组名唯一（`uq_agent_group_dept_name`）。`department_id` 为空时
  SQLite/PG 都允许多行同名——公司直属组由业务层按名称去重。
"""
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from .base import TimestampMixin

if TYPE_CHECKING:
    from .agent import Agent


class AgentGroup(TimestampMixin, Base):
    __tablename__ = "agent_groups"
    __table_args__ = (
        UniqueConstraint("department_id", "name", name="uq_agent_group_dept_name"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    department_id: Mapped[str | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True)

    agents: Mapped[list["Agent"]] = relationship(back_populates="group")
