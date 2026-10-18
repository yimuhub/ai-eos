"""设备授权（V5.4 配对码模型）

手机扫码接入时登记「这台设备」：
  - 配对 token 一次性（10 分钟 TTL + 用后即焚），换出带 device_id 的
    refresh token（库里有记录）；
  - 老板在桌面端看到所有接入设备，可一键吊销——
    吊销 = 该设备的 refresh_tokens 全部 revoked + devices.revoked，
    短期 access JWT 自然过期（无状态，不追杀）。
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(120))          # 从 UA 提取（如 "Safari on iPhone"）
    fingerprint: Mapped[str | None] = mapped_column(String(190))   # UA 前缀 | IP 前 24 位 | nonce
    source: Mapped[str] = mapped_column(String(20), default="pairing")  # pairing | login
    last_seen_at: Mapped[datetime | None] = mapped_column()
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime | None] = mapped_column(default=_utcnow)


class PairingToken(Base):
    """一次性配对码：桌面端生成 → 手机扫码换登录。"""
    __tablename__ = "pairing_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        default=lambda: _utcnow() + timedelta(minutes=10))
    used_at: Mapped[datetime | None] = mapped_column()
    device_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("devices.id"))

    @staticmethod
    def new() -> tuple[str, "PairingToken"]:
        """返回 (明文 token, 待 add 的行)。明文只出现这一次，库里只存 SHA-256。"""
        import hashlib
        raw = secrets.token_urlsafe(24)
        row = PairingToken(token_hash=hashlib.sha256(raw.encode()).hexdigest())
        return raw, row
