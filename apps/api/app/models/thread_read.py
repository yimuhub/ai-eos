"""会话已读位置（C3）：读状态跨设备同步。

## 为什么已读要落库

左栏的置顶/归档是"这台设备怎么摆"，已读却是**用户对内容的判断**：
在手机 `/m` 上看完一条作战会话，桌面左栏不应该还亮着未读点。
localStorage 天然做不到这件事，所以只把已读时间戳同步到服务端，
置顶/归档仍留在本机。

## 语义

* 一行 = 一个用户在一家公司里对某个会话（`war:<id>` / `meeting:<id>` /
  `single:<agent_id>` / `group:<project_id>`）的已读时间；
* 时间戳是**客户端毫秒**，合并规则只增不减（`max`），时钟回拨不会把
  已读退回未读；
* 唯一约束 `(user_id, company_id, thread_key)` 是 upsert 的依据，
  也让并发写入最多撞约束、不会产生重复行。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, DateTime, ForeignKey, Index, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    # naive UTC：与 notifications/intervention 一致，避免 aware/naive 混排
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ThreadRead(Base):
    __tablename__ = "thread_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "company_id", "thread_key",
                         name="uq_thread_reads_scope"),
        Index("ix_thread_reads_user_company", "user_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    thread_key: Mapped[str] = mapped_column(String(160))
    read_at_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
