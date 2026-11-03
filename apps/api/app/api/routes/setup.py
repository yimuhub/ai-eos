"""首次安装向导接口（P5）。

## 为什么是独立路由而不是塞进 auth

`/setup` 只在"一个超管都还没有"的窗口期可用，窗口关闭后永远 409；
它创建的是平台的第一个超管 + 第一家公司和 owner 成员关系。
把这套语义混进登录/注册路由里，会让"注册"这条高频路径多背一份
一次性初始化逻辑，也让权限审计难以一眼看清谁能创建超管。

## 幂等与并发

"是否已初始化"的唯一事实源是 `users.is_superadmin = true`。
判定在**同一个写事务内、提交前**做两次（开头一次快速失败、写完再一次
兜底并发）：SQLite 只允许一个写者，第二个请求的 INSERT 会阻塞到第一个
提交，随后的复检必然能看到对方；PostgreSQL 上额外用事务级 advisory lock
串行化两个安装请求，避免 READ COMMITTED 下双方都看不到对方未提交的行。

## 单事务

User / Company / CompanyMember / event_log 全部只 flush 不 commit，
任何一步失败都整体回滚——绝不允许留下"有用户没有公司"的半套安装。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.auth import validate_password_strength
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.security import hash_password, issue_token_pair
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig, Model
from app.models.company import Company, Department
from app.models.device import Device, PairingToken
from app.models.membership import CompanyMember
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.event_system import log_event

logger = get_logger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])

# 与 /auth/register 完全一致的口径：冲突时不泄露"是邮箱还是用户名被占用"
_DUP_MSG = "该邮箱或用户名已被注册，请更换后重试"
_ALREADY_MSG = "already initialized"

# PG 事务级 advisory lock 的键（"AIEOS" 的十六进制）。只在安装窗口期使用，
# 提交/回滚即自动释放，不会影响其它业务。
_SETUP_LOCK_KEY = 0x4149454F53

# ---------- 安装端点限流 ----------
# 照 login 的写法：Redis 优先（多实例一致），连不上回退进程内存计数。
# 阈值/窗口与登录限流相同（5 分钟 10 次，按客户端 IP + 端点分别计数）。
# 这两个端点免鉴权，不限速就等于给匿名访客一个反复锤数据库的入口。
_RATE_WINDOW_SECONDS = 300
_RATE_MAX_REQUESTS = 10
_rate_hits: dict[str, tuple[int, float]] = {}
_rate_redis_client = None
_rate_redis_probed = False


async def _rate_redis():
    """返回可用的异步 Redis 客户端；不可用返回 None（调用方走内存回退）。"""
    global _rate_redis_client, _rate_redis_probed
    if _rate_redis_probed:
        return _rate_redis_client
    _rate_redis_probed = True
    try:
        import redis.asyncio as aioredis
        client = aioredis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        await asyncio.wait_for(client.ping(), timeout=3)
    except Exception as exc:  # noqa: BLE001 — 没有 Redis 是正常形态
        logger.warning("安装限流：Redis 不可用（%s），回退进程内存实现：%s",
                       settings.redis_url, exc)
        _rate_redis_client = None
    else:
        _rate_redis_client = client
        logger.info("安装限流：使用 Redis（%s）", settings.redis_url)
    return _rate_redis_client


def _rate_key(request: Request, scope: str) -> str:
    ip = ""
    if request is not None and request.client:
        ip = request.client.host or ""
    return f"{(ip or '').strip()}|setup:{scope}"


def _too_many(wait_seconds: float) -> HTTPException:
    return HTTPException(
        429, f"安装请求过于频繁，请 {max(int(wait_seconds), 1)} 秒后重试。")


def _hit_memory(key: str) -> None:
    now = time.time()
    count, first = _rate_hits.get(key, (0, now))
    if now - first > _RATE_WINDOW_SECONDS:
        count, first = 0, now
    count += 1
    _rate_hits[key] = (count, first)
    if count > _RATE_MAX_REQUESTS:
        raise _too_many(_RATE_WINDOW_SECONDS - (now - first))


async def _enforce_rate_limit(request: Request, scope: str) -> None:
    """超过阈值则 429，并告知等待秒数。"""
    global _rate_redis_client, _rate_redis_probed
    key = _rate_key(request, scope)
    r = await _rate_redis()
    if r is None:
        _hit_memory(key)
        return
    rk = f"aieos:setup_rate:{key}"
    try:
        count = await r.incr(rk)
        if count == 1:
            await r.expire(rk, _RATE_WINDOW_SECONDS)
        if count > _RATE_MAX_REQUESTS:
            ttl = await r.ttl(rk)
            raise _too_many(ttl if isinstance(ttl, (int, float)) and ttl > 0
                            else _RATE_WINDOW_SECONDS)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — Redis 中途故障降级而非拒绝服务
        _rate_redis_probed = False
        _rate_redis_client = None
        logger.warning("安装限流 Redis 读写失败，本次降级内存实现：%s", exc)
        _hit_memory(key)


async def _has_superadmin(db: AsyncSession,
                          exclude_user_id: str | None = None) -> bool:
    """是否存在平台超管（可排除本次刚插入的用户，用于提交前复检）。"""
    stmt = select(User.id).where(User.is_superadmin.is_(True))
    if exclude_user_id:
        stmt = stmt.where(User.id != exclude_user_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def _serialize_setup(db: AsyncSession) -> None:
    """在支持的方言上把并发安装请求串行化。

    SQLite 的写锁本身已经把两个安装请求排成先后；PostgreSQL 需要显式锁，
    否则两个 READ COMMITTED 事务可能都看不到对方未提交的超管行。
    """
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                         {"k": _SETUP_LOCK_KEY})


# 用户名规则与 /auth/register 完全一致（2~50 字符，去首尾空白）
class SetupIn(BaseModel):
    username: str
    email: EmailStr
    password: str
    display_name: str | None = None
    company_name: str
    description: str | None = None

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
        # 复用注册的密码规则：安装出来的是超管，口径只能一致或更严
        return validate_password_strength(v)

    @field_validator("company_name")
    @classmethod
    def _check_company_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("公司名称不能为空")
        if len(v) > 200:
            raise ValueError("公司名称最多 200 个字符")
        return v

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 100:
            raise ValueError("显示名称最多 100 个字符")
        return v


@router.get("/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """是否还需要安装：数据库里一个平台超管都没有时为 True。

    **不限流**：这是首页/登录页/安装页每次加载都会调的只读探针（一条索引
    查询）。按 5 分钟 10 次限流意味着用户刷新几次就会被 429，而前端把探测
    失败当作"已初始化"处理——全新用户会永远停在登录页，再也进不去安装向导。
    POST /setup 是写路径且一次性，仍保留限流。
    """
    return {"needs_setup": not await _has_superadmin(db)}


@router.post("", status_code=201)
async def initialize(data: SetupIn, request: Request,
                     db: AsyncSession = Depends(get_db)):
    """创建首个超管 + 公司 + owner 成员关系，并直接签发登录令牌。"""
    await _enforce_rate_limit(request, "initialize")
    await _serialize_setup(db)

    # 快速失败：已经装过就不再浪费一次写事务
    if await _has_superadmin(db):
        raise HTTPException(409, _ALREADY_MSG)

    exists = await db.execute(
        select(User).where((User.email == data.email) | (User.username == data.username))
    )
    if exists.scalar_one_or_none():
        raise HTTPException(409, _DUP_MSG)

    try:
        user = User(
            email=data.email,
            username=data.username,
            password_hash=hash_password(data.password),
            display_name=data.display_name or data.username,
            is_superadmin=True,
            is_active=True,
        )
        db.add(user)
        await db.flush()  # 拿到用户 id（uuid 默认值在 flush 时生成）

        company = Company(
            owner_id=user.id,
            name=data.company_name,
            description=data.description,
        )
        db.add(company)
        await db.flush()

        db.add(CompanyMember(
            company_id=company.id,
            user_id=user.id,
            role="owner",
            status="active",
        ))
        await db.flush()

        await log_event(
            db, "setup.initialized", "organization", None, "user", user.id,
            {"company_id": company.id, "company_name": company.name},
        )

        # 提交前在同一事务内再查一次：首次判定之后可能有并发安装先提交。
        # 排除本次刚插入的用户，否则永远"查到自己"。
        if await _has_superadmin(db, exclude_user_id=user.id):
            await db.rollback()
            raise HTTPException(409, _ALREADY_MSG)

        # issue_token_pair 只 flush 不 commit，提交权仍在本事务
        pair = await issue_token_pair(db, user.id)
        await db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        # 并发下邮箱/用户名被同时占用：唯一约束是最后一道兜底
        await db.rollback()
        raise HTTPException(409, _DUP_MSG)
    except Exception:
        await db.rollback()
        raise

    # 日志只记录标识，不打印密码/token 原文
    logger.info("安装向导完成：超管 username=%s user=%s company=%s",
                user.username, user.id, company.id)

    return {
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "is_superadmin": bool(user.is_superadmin),
        },
        "company": {"id": company.id, "name": company.name},
    }


class RebuildIn(BaseModel):
    # 必须原样输入公司名：重建会删除整家公司与管理员账号，防误触
    company_name: str


@router.post("/rebuild", status_code=204)
async def rebuild(data: RebuildIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  user=Depends(get_current_user)):
    """推倒重建：把这次安装出来的东西删干净，回到"未初始化"。

    只允许**唯一超管**调用，且要求原样输入公司名。删除范围就是他名下的
    全部公司（含部门 / 员工 / 项目 / 成果…，靠外键级联）与他自己的账号；
    删完系统里没有超管，`GET /setup/status` 会重新返回 needs_setup=true，
    安装向导从第 1 步重来。

    为什么删除账号而不是保留：保留会让邮箱/用户名继续被占用，用户想用
    "同一个老板账号重新装一遍"会撞唯一约束；而留着无公司的账号也进不了
    任何页面，只是脏数据。
    """
    await _enforce_rate_limit(request, "rebuild")
    if not user.is_superadmin:
        raise HTTPException(404, "Not found")

    supers = (await db.execute(
        select(func.count()).select_from(User).where(
            User.is_superadmin.is_(True)))).scalar_one()
    if supers > 1:
        # 多超管说明系统已经进入正常协作阶段，重建必须走公司管理的删除流程
        raise HTTPException(409, "系统里已有多个超级管理员，不能整体重建；请在公司管理里逐个删除")

    companies = (await db.execute(
        select(Company).where(Company.owner_id == user.id))).scalars().all()
    if not companies:
        raise HTTPException(409, "没有可重建的公司")
    names = {c.name for c in companies}
    if data.company_name.strip() not in names:
        raise HTTPException(400, "公司名称不匹配，未执行重建")

    # user 的属性在 rollback/commit 后会过期，再读就是事务外的懒加载
    # （异步会话里直接抛 MissingGreenlet，把本该是 409 的结果变成 500），
    # 所以提前把要用的字段取出来。
    user_id = str(user.id)
    try:
        # 组织这几张表的外键**没有级联**：`Department.agents` 没配 cascade，
        # 而安装向导绑模型、AI 生成组织落地都会写 agent_model_configs。
        # ORM 删公司时只带走部门，员工行留在原地 → `DELETE FROM departments`
        # 撞 FOREIGN KEY，表现为「推倒重建」点了没反应。
        # 先按"模型绑定 → 员工"清掉，部门和公司仍由 ORM 带走；
        # 其它业务数据（任务/记忆…）若还引用这些员工会抛 IntegrityError，
        # 由下面的分支如实返回 409。
        company_ids = [c.id for c in companies]
        await db.execute(sa_delete(AgentModelConfig).where(or_(
            AgentModelConfig.agent_id.in_(
                select(Agent.id).where(Agent.company_id.in_(company_ids))),
            AgentModelConfig.model_id.in_(
                select(Model.id).where(Model.company_id.in_(company_ids))),
        )))
        await db.execute(sa_delete(Agent).where(Agent.company_id.in_(company_ids)))
        # 部门之间还有父子关系（AI 生成的组织是「中心 → 部门」两层），
        # 而 parent_department_id 同样不级联：必须**叶子先删**，
        # 否则删中心会被挂在下面的部门顶住外键。
        dept_rows = (await db.execute(
            select(Department.id, Department.parent_department_id).where(
                Department.company_id.in_(company_ids)))).all()
        alive = {d for d, _ in dept_rows}
        while alive:
            referenced = {p for d, p in dept_rows if d in alive and p in alive}
            ready = [d for d in alive if d not in referenced]
            if not ready:
                break  # 父子成环：交给下面的 IntegrityError 分支如实报错
            await db.execute(sa_delete(Department).where(Department.id.in_(ready)))
            alive -= set(ready)

        for c in companies:
            await db.delete(c)
        await db.flush()

        # 设备/配对码的外键没有级联，先显式清掉；refresh token 由 users 级联
        await db.execute(sa_delete(PairingToken).where(PairingToken.user_id == user_id))
        await db.execute(sa_delete(Device).where(Device.user_id == user_id))
        await db.execute(sa_delete(CompanyMember).where(CompanyMember.user_id == user_id))
        await db.execute(sa_delete(RefreshToken).where(RefreshToken.user_id == user_id))
        await db.delete(user)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.warning("重建失败：仍有其它数据引用该账号 user=%s", user_id)
        raise HTTPException(409, "系统里已有与这个账号关联的其它数据，无法整体重建")

    logger.info("推倒重建：已删除超管 user=%s 与公司 %s", user_id, sorted(names))
