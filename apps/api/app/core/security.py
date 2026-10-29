"""Password hashing + JWT helpers + refresh token lifecycle."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {"sub": subject, "exp": expire},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload.get("sub")
    except Exception:
        return None


# ---------------- Refresh token（有状态，可吊销） ----------------
# access token 仍是无状态 JWT；refresh token 是随机字符串，
# 库里只存其 SHA-256 哈希（模型见 app/models/refresh_token.py）。

def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    # 与项目其它手写时间字段一致：用 naive UTC，
    # 避免 SQLite 下 aware/naive 比较炸。
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def issue_token_pair(db: AsyncSession, user_id: str,
                           device_id: str | None = None) -> dict[str, str]:
    """签发一对 token：短期 access（JWT）+ 长期 refresh（DB 可吊销）。

    只 add + flush，不 commit——提交权交还调用方，不破坏外层事务。
    device_id（V5.4）：设备授权场景传入，吊销设备时可批量吊销其 refresh。
    """
    from app.models.refresh_token import RefreshToken

    raw = secrets.token_urlsafe(48)
    db.add(RefreshToken(
        user_id=user_id,
        token_hash=_hash_refresh_token(raw),
        expires_at=_utcnow_naive() + timedelta(days=settings.jwt_refresh_expire_days),
        device_id=device_id,
    ))
    await db.flush()
    return {"access_token": create_access_token(user_id), "refresh_token": raw}


async def rotate_refresh_token(
        db: AsyncSession, raw_token: str) -> tuple[str, str, str] | None:
    """验 refresh_token：有效则吊销旧的并签发新对。

    返回 (access_token, refresh_token, user_id)；无效/过期/已吊销返回 None。
    同样只 flush 不 commit。
    """
    from app.models.refresh_token import RefreshToken

    token_hash = _hash_refresh_token(raw_token or "")
    now = _utcnow_naive()
    # 用条件 UPDATE 抢占吊销权（CAS）：两个并发刷新拿着同一个 refresh token
    # 时，先到者把 revoked 置真，后到者的 WHERE 不成立 → rowcount=0 → 拒绝。
    # 改造前是"先 SELECT 判断、再赋值"，两个请求能同时通过，等于 token 可复用。
    claimed = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash,
               RefreshToken.revoked.is_(False),
               RefreshToken.expires_at > now)
        .values(revoked=True)
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        return None
    row = (await db.execute(select(RefreshToken).where(
        RefreshToken.token_hash == token_hash))).scalar_one()
    pair = await issue_token_pair(db, row.user_id)
    await db.flush()
    return pair["access_token"], pair["refresh_token"], row.user_id


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> bool:
    """吊销一个 refresh token。存在且未吊销返回 True，否则 False。"""
    from app.models.refresh_token import RefreshToken

    row = (await db.execute(select(RefreshToken).where(
        RefreshToken.token_hash == _hash_refresh_token(raw_token or "")
    ))).scalar_one_or_none()
    if row is None or row.revoked:
        return False
    row.revoked = True
    await db.flush()
    return True
