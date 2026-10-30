"""Auth endpoints."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.security import (hash_password, issue_token_pair,
                               revoke_refresh_token, rotate_refresh_token,
                               verify_password)
from app.db.session import get_db
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# 密码最小长度。原实现只要求"是字符串"，空串/单字符也能注册——
# 对一个保存着公司全部经营数据与模型密钥的系统来说这是不可接受的。
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128   # 上限防止超长输入拖垮 bcrypt

# ---------- 登录失败限流 ----------
# Redis 优先：多实例/重启后计数不丢；Redis 连不上时回退进程内存 dict。
# 对外行为不变：键为 "identity|ip"，5 分钟窗口 / 10 次 / 429 告知等待秒数。
# （现有测试直接读写 _login_failures，内存回退实现必须原样保留。）
_FAIL_WINDOW_SECONDS = 300      # 统计窗口 5 分钟
_MAX_FAILS = 10                 # 窗口内最多失败 10 次
_login_failures: dict[str, tuple[int, float]] = {}

_redis_client = None
_redis_probed = False


async def _rate_redis():
    """返回可用的异步 Redis 客户端；不可用则返回 None（调用方走内存回退）。

    此前用的是同步 redis 客户端：get/incr/ttl 都是阻塞 IO，
    在 async 路由里会卡住整个事件循环。改用 redis.asyncio 后
    探测/读写全程 await，不再阻塞。探测只做一次并缓存：
    便携形态下没有 Redis 服务，不应每次登录都等超时。
    """
    global _redis_client, _redis_probed
    if _redis_probed:
        return _redis_client
    _redis_probed = True
    try:
        import redis.asyncio as aioredis
        client = aioredis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        # ping 加总超时兜底：连不上/DNS 挂起都不能拖住登录
        await asyncio.wait_for(client.ping(), timeout=3)
    except Exception as exc:
        logger.warning("登录限流：Redis 不可用（%s），回退进程内存实现：%s",
                       settings.redis_url, exc)
        _redis_client = None
    else:
        _redis_client = client
        logger.info("登录限流：使用 Redis（%s）", settings.redis_url)
    return _redis_client


def _redis_rate_key(key: str) -> str:
    return f"aieos:login_fail:{key}"


async def _check_rate_limit(key: str) -> None:
    """超过阈值则拒绝，并明确告知还需等待多久。"""
    global _redis_client, _redis_probed
    r = await _rate_redis()
    if r is None:
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
        return
    rk = _redis_rate_key(key)
    try:
        count = int((await r.get(rk)) or 0)
        if count < _MAX_FAILS:
            return
        wait = await r.ttl(rk)
    except Exception as exc:
        # Redis 探测成功后中途故障（重启/网络抖动）：降级内存计数而不是拒绝登录，
        # 并重新探测（下次登录再尝试连 Redis）
        _redis_probed = False
        _redis_client = None
        logger.warning("登录限流 Redis 读写失败，本次降级内存实现：%s", exc)
        return _check_memory(key)


def _check_memory(key: str) -> None:
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


async def _record_failure(key: str) -> None:
    global _redis_client, _redis_probed
    r = await _rate_redis()
    if r is None:
        now = time.time()
        count, first = _login_failures.get(key, (0, now))
        if now - first > _FAIL_WINDOW_SECONDS:
            count, first = 0, now
        _login_failures[key] = (count + 1, first)
        return
    rk = _redis_rate_key(key)
    try:
        count = await r.incr(rk)
        if count == 1:
            # 窗口锚定在首次失败（与内存实现语义一致），而非滑动窗口
            await r.expire(rk, _FAIL_WINDOW_SECONDS)
    except Exception as exc:
        _redis_probed = False
        _redis_client = None
        logger.warning("登录限流 Redis 计数失败，本次记入内存：%s", exc)
        now = time.time()
        count, first = _login_failures.get(key, (0, now))
        if now - first > _FAIL_WINDOW_SECONDS:
            count, first = 0, now
        _login_failures[key] = (count + 1, first)


async def _clear_failures(key: str) -> None:
    global _redis_client, _redis_probed
    r = await _rate_redis()
    if r is None:
        _login_failures.pop(key, None)
        return
    try:
        await r.delete(_redis_rate_key(key))
    except Exception as exc:
        _redis_probed = False
        _redis_client = None
        logger.warning("登录限流 Redis 清理失败，本次清内存计数：%s", exc)
        _login_failures.pop(key, None)

# 唯一性冲突时为了不泄露"哪个字段被占用"，统一给一句提示
_DUP_MSG = "该邮箱或用户名已被注册，请更换后重试"


def validate_password_strength(password: str) -> str:
    """注册与安装向导共用的密码强度规则。

    抽成模块级函数是为了让 `/setup` 与 `/auth/register` 用**同一份**
    规则：两处各写一遍迟早会漂移，而安装向导创建的是平台超管，
    密码要求只应更严、不应更松。行为与旧的内联校验完全一致。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"密码最多 {MAX_PASSWORD_LENGTH} 位")
    # 只要求"字母 + 数字"，避免过度复杂的规则让用户写便利贴
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("密码需同时包含字母和数字")
    return password


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
        return validate_password_strength(v)


class LoginIn(BaseModel):
    identity: str  # 支持邮箱或用户名
    password: str


def _rate_key(identity: str, request: Request | None) -> str:
    ip = ""
    if request is not None and request.client:
        ip = request.client.host or ""
    return f"{(identity or '').strip().lower()}|{ip}"


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
    await _check_rate_limit(key)

    result = await db.execute(
        select(User).where((User.email == data.identity) | (User.username == data.identity))
    )
    user = result.scalar_one_or_none()
    # 用户不存在与密码错误返回同一提示，避免账号枚举
    if not user or not verify_password(data.password, user.password_hash):
        await _record_failure(key)
        raise HTTPException(401, "邮箱/用户名或密码不正确")
    if not user.is_active:
        raise HTTPException(403, "该账号已被停用")

    await _clear_failures(key)
    pair = await issue_token_pair(db, user.id)
    await db.commit()
    return {
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "username": user.username,
                 "display_name": user.display_name},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "username": user.username,
            "display_name": user.display_name, "avatar_url": user.avatar_url}


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh(data: RefreshIn, db: AsyncSession = Depends(get_db)):
    """用 refresh_token 轮换出一对新 token。

    响应约定：{"access_token", "refresh_token"}——前端按此做 401 自动刷新。
    旧 refresh_token 在轮换成功时立即吊销（一 token 只用一次）。
    """
    rotated = await rotate_refresh_token(db, data.refresh_token)
    if rotated is None:
        raise HTTPException(401, "refresh_token 无效或已过期，请重新登录")
    access_token, new_refresh_token, user_id = rotated
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        # 账号被删/停用：refresh 链必须断（旧 token 已在轮换中被吊销）
        await db.commit()
        raise HTTPException(401, "用户不存在或已被停用")
    await db.commit()
    logger.info("refresh_token 已轮换 user=%s", user_id)
    return {"access_token": access_token, "refresh_token": new_refresh_token}


class LogoutIn(BaseModel):
    refresh_token: str | None = None


@router.post("/logout")
async def logout(data: LogoutIn | None = None,
                 db: AsyncSession = Depends(get_db)):
    """登出：吊销传来的 refresh_token（若有）。

    access token 仍是无状态 JWT，客户端丢弃即可；其短期有效期是最终兜底。
    不传 refresh_token 时行为与旧版一致（仅返回 logged out）。
    """
    if data and data.refresh_token:
        await revoke_refresh_token(db, data.refresh_token)
        await db.commit()
        logger.info("用户登出，已吊销其 refresh_token")
    return {"detail": "logged out"}
