"""Web Push 订阅（C6）：每台浏览器/设备一行。

## 为什么 endpoint 全局唯一

浏览器推送服务分配的 endpoint 就是这台设备的"地址"，同一个 endpoint
重复订阅只应该更新密钥（浏览器会轮换 p256dh/auth），而不是堆出多行——
否则一次通知会被同一台手机收到 N 次。

## 长度

Chrome/Firefox 的 endpoint 实测已超过 500 字符，历史实现里 `String(500)`
在 PG 上会直接溢出。这里统一给到 2000；p256dh/auth 也留出余量，
避免浏览器升级后密钥变长导致整条通知链路失败。

## 索引

* `endpoint` 唯一：订阅幂等 upsert 的依据；
* `user_id`：`send_to_user` 按用户取全部设备；
* `company_id`：租户清理/审计时按公司扫描。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    """naive UTC：与 notification/meeting 等模块保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(
        String(2000), unique=True, index=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(128))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
