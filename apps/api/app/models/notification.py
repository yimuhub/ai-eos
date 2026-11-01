"""站内通知（C4）：完成 / 失败 / 待审批三类事件必须到达用户。

## 为什么通知行要带 company_id 而不是只挂 user

同一个人可以属于多家公司。前端顶栏的铃铛永远只显示"当前公司"的未读数；
不落 company_id 的话，切换公司后要么把 A 公司的待办混进 B 公司，
要么得在查询时联表反查归属，多一次 join 且容易漏过滤。

## 索引

* `(user_id, is_read)`：铃铛轮询的未读数查询——最高频、必须走索引；
* `(company_id, created_at)`：列表按公司 + 时间倒序取最近 N 条；
  也用于将来按公司清理历史通知。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    """naive UTC：与 intervention/meeting 等模块的写法一致。

    SQLite 的 DATETIME 不保存时区，混用 aware/naive 会让
    `ORDER BY created_at` 的字典序出错（'+00:00' 参与比较）。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_unread", "user_id", "is_read"),
        Index("ix_notifications_company_created", "company_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    # task.completed / task.failed / approval.pending / meeting.minutes
    # 用字符串而不是 Enum：新增通知类型不应该需要重建表（SQLite 的
    # Enum 会落成 CHECK 约束）。
    type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
