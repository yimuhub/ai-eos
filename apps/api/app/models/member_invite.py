"""成员邀请链接：老板发一条链接，对方自己注册/登录后加入公司。

## 为什么不用「填邮箱 + 替对方设密码」

旧入口要求邀请人替被邀请人想一个密码，再把密码用微信/口头传过去——
这既是明文口令传输，也让「邀请」变成「代注册」。链接邀请把这两件事拆开：
老板只授予**加入资格**，账号与密码由被邀请人自己掌握。

## 安全边界

* 明文 token 只出现在创建响应里一次，库里只存 SHA-256；
* 一次性（`used_at`）、可撤销（`revoked_at`）、有过期时间；
* 角色只收 member / viewer / company_admin——不允许通过链接授予 owner；
* `company_admin` 链接只有 OWNER 能生成（与成员接口同一口径）。
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from .base import TimestampMixin

INVITE_TTL_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MemberInvite(TimestampMixin, Base):
    __tablename__ = "member_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False,
                                      default="member")
    # 部门被删除时邀请仍可用，只是不再带入部门（SET NULL）
    department_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("departments.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: utcnow() + timedelta(days=INVITE_TTL_DAYS))
    used_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime())

    @staticmethod
    def new(company_id: str, role: str, department_id: str | None,
            created_by: str | None, days: int = INVITE_TTL_DAYS,
            ) -> tuple[str, "MemberInvite"]:
        """返回 (明文 token, 待 add 的行)。明文只出现这一次。"""
        raw = secrets.token_urlsafe(32)
        row = MemberInvite(
            company_id=company_id,
            token_hash=MemberInvite.hash_token(raw),
            role=role,
            department_id=department_id,
            created_by=created_by,
            expires_at=utcnow() + timedelta(days=days),
        )
        return raw, row

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    def state(self) -> tuple[str, str | None]:
        """返回 (状态, 不可用原因)。状态：valid / used / revoked / expired。"""
        if self.used_at is not None:
            return "used", "邀请链接已被使用"
        if self.revoked_at is not None:
            return "revoked", "邀请链接已撤销"
        if self.expires_at is not None and self.expires_at <= utcnow():
            return "expired", "邀请链接已过期，请让管理员重新生成"
        return "valid", None
