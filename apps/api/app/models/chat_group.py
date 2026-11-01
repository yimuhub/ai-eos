"""自由群聊：老板拉任意 Agent 成组，不再只挂在项目下。

- kind=preset：内置分组（营销 / 产品研发），成员来自 content/chat_groups/*.json；
  首次进入时缺的员工会自动生成（绑定专家库人设），之后可照常培训升级。
- kind=custom：老板自己建组、自己选人。

群聊消息仍存 group_chat_messages（scope="group", scope_id=本表 id）。
"""
from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin


class ChatGroup(TimestampMixin, Base):
    __tablename__ = "chat_groups"
    # 同一公司同一内置分组只能有一行；preset_id 为 NULL 的自建群在 SQLite/Postgres
    # 下都不参与唯一性（多个 NULL 合法），不受影响。
    __table_args__ = (
        UniqueConstraint("company_id", "preset_id",
                         name="uq_chat_group_company_preset"),
    )

    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # preset | custom
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="custom")
    # 内置分组的稳定标识（marketing / product-rd）；自建组为空
    preset_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    agent_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
