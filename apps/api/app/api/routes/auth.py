"""Auth endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.logging_config import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# 密码最小长度。原实现只要求"是字符串"，空串/单字符也能注册——
# 对一个保存着公司全部经营数据与模型密钥的系统来说这是不可接受的。
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128   # 上限防止超长输入拖垮 bcrypt

# ---------- 登录失败限流 ----------
# 简单内存实现：键为 "identity|ip"，记录 (失败次数, 首次失败时间)。
# 单进程部署（本项目的便携形态）足够用；多实例部署应换成 Redis 计数。
_FAIL_WINDOW_SECONDS = 300      # 统计窗口 5 分钟
_MAX_FAILS = 10                 # 窗口内最多失败 10 次
_login_failures: dict[str, tuple[int, float]] = {}

# 唯一性冲突时为了不泄露"哪个字段被占用"，统一给一句提示
_DUP_MSG = "该邮箱或用户名已被注册，请更换后重试"


class RegisterIn(BaseModel):
    email: EmailStr
    username: str
    password: str
    display_name: str | None = None

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 2:
            raise ValueError("用户名至少 2 个字符")
        if len(v) > 50:
            raise ValueError("用户名最多 50 个字符")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
        if len(v) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"密码最多 {MAX_PASSWORD_LENGTH} 位")
        # 只要求"字母 + 数字"，避免过度复杂的规则让用户写便利贴
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("密码需同时包含字母和数字")
        return v


class LoginIn(BaseModel):
    identity: str  # 支持邮箱或用户名
    password: str


def _rate_key(identity: str, request: Request | None) -> str:
    ip = ""
    if request is not None and request.client:
        ip = request.client.host or ""
    return f"{(identity or '').strip().lower()}|{ip}"


def _check_rate_limit(key: str) -> None:
    """超过阈值则拒绝，并明确告知还需等待多久。"""
    now = time.time()
    count, first = _login_failures.get(key, (0, now))
    if now - first > _FAIL_WINDOW_SECONDS:
        _login_failures.pop(key, None)
        return
    if count >= _MAX_FAILS:
        wait = int(_FAIL_WINDOW_SECONDS - (now - first))
        raise HTTPException(
            429,
            f"登录失败次数过多，请 {max(wait, 1)} 秒后重试。")


def _record_failure(key: str) -> None:
    now = time.time()
    count, first = _login_failures.get(key, (0, now))
    if now - first > _FAIL_WINDOW_SECONDS:
        count, first = 0, now
    _login_failures[key] = (count + 1, first)


def _clear_failures(key: str) -> None:
    _login_failures.pop(key, None)


@router.post("/register", status_code=201)
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)):
    exists = await db.execute(
        select(User).where((User.email == data.email) | (User.username == data.username))
    )
    if exists.scalar_one_or_none():
        raise HTTPException(409, _DUP_MSG)
    user = User(
        email=data.email,
        username=data.username,
        password_hash=hash_password(data.password),
        display_name=data.display_name or data.username,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("新用户注册成功 username=%s", user.username)
    return {"id": user.id, "email": user.email, "username": user.username}


@router.post("/login")
async def login(data: LoginIn, request: Request = None,
                db: AsyncSession = Depends(get_db)):
    """登录：支持邮箱或用户名。

    改造新增：失败次数限流。原实现可以无限次尝试密码，
    对一个本机部署、保存着模型密钥与企业数据的系统风险过高。
    """
    key = _rate_key(data.identity, request)
    _check_rate_limit(key)

    result = await db.execute(
        select(User).where((User.email == data.identity) | (User.username == data.identity))
    )
    user = result.scalar_one_or_none()
    # 用户不存在与密码错误返回同一提示，避免账号枚举
    if not user or not verify_password(data.password, user.password_hash):
        _record_failure(key)
        raise HTTPException(401, "邮箱/用户名或密码不正确")
    if not user.is_active:
        raise HTTPException(403, "该账号已被停用")

    _clear_failures(key)
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "username": user.username,
                 "display_name": user.display_name},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "username": user.username,
            "display_name": user.display_name, "avatar_url": user.avatar_url}


@router.post("/logout")
async def logout():
    # JWT is stateless — client drops the token.
    return {"detail": "logged out"}
