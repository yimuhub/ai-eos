"""远程设备的接入与命令下发。

## 为什么用 HTTP 长轮询而不是 WebSocket

sidecar 只需要三个动作：报心跳、领命令、交结果。用长轮询（领命令时服务端最多
等若干秒）就能拿到"接近实时"的下发，同时：
* 鉴权沿用现有请求头依赖，不必为 WS 另开一条带 token 的路；
* 任何反代/内网穿透都透传 HTTP，不用额外配置升级协议；
* 便携形态单进程 + SQLite，队列正确性靠数据库的 CAS 保证，进程重启不丢命令。

代价是最坏情况多等一个轮询间隔，对"点一下按钮让另一台机器改个文件"这种操作
完全够用。

## 两把锁

后端策略（`computer_policy`）判「这个动作该不该做」，sidecar 自己再判一次
「这台机器允不允许」。**设备端不受服务端策略约束的能力，才是它真实的边界**——
所以领取到的命令在设备本地还要过一遍路径与命令护栏（见 apps/device_agent）。
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.computer_device import ComputerDevice, ComputerDispatch

logger = get_logger(__name__)

# 接入码有效期：装 sidecar 要在目标机器上敲命令，10 分钟不够用
ENROLL_TTL = timedelta(minutes=30)
# 一次下发等结果的上限。改一个文件、跑一条命令都在这个窗口内；
# 超过就判过期（设备可能已经掉线），审计里留下"未回报"而不是伪造成功。
DEFAULT_DISPATCH_TIMEOUT = timedelta(seconds=120)
MIN_DISPATCH_TIMEOUT = timedelta(seconds=5)
MAX_DISPATCH_TIMEOUT = timedelta(seconds=600)
# 结果正好压在 deadline 上回报时不该被判没回报，多等这一小段
RESULT_GRACE_SECONDS = 5.0
# 领命令时服务端最多替设备等这么久（之后返回空，设备立刻再问一次）
CLAIM_HOLD_SECONDS = 12.0
# 队列侧的轮询粒度：下发方与领取方都用它，最坏延迟 ≈ 2 × 该值
POLL_INTERVAL = 0.25


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# ---------------------------------------------------------------- 老板侧


async def create_device(db: AsyncSession, *, company_id: str, name: str,
                        created_by: str | None = None) -> tuple[ComputerDevice, str]:
    """登记一台待接入设备，返回 (行, 明文接入码)。

    接入码明文只在这里出现一次，库里只存 SHA-256（同配对码的口径）。
    """
    raw = secrets.token_urlsafe(16)
    device = ComputerDevice(
        id=str(uuid.uuid4()), company_id=company_id, name=name.strip(),
        enroll_code_hash=_hash(raw), enroll_expires_at=_now() + ENROLL_TTL,
        revoked=False, capabilities=[], created_by=created_by)
    db.add(device)
    await db.commit()
    logger.info("远程设备已登记 company=%s device=%s", company_id, device.id)
    return device, raw


async def renew_enrollment(db: AsyncSession, device: ComputerDevice) -> str:
    """接入码过期/丢失时换一张新的：旧码立刻失效，避免两张码同时可用。"""
    raw = secrets.token_urlsafe(16)
    device.enroll_code_hash = _hash(raw)
    device.enroll_expires_at = _now() + ENROLL_TTL
    await db.commit()
    logger.info("设备接入码已重置 device=%s", device.id)
    return raw


async def revoke_device(db: AsyncSession, device: ComputerDevice) -> int:
    """吊销设备：密钥作废 + 排队中的命令全部判过期。

    返回被取消的命令数。已 claimed 的不强杀——本机进程正在跑，
    只能等它自己回报或超时。
    """
    device.revoked = True
    device.secret_hash = None
    device.enroll_code_hash = None
    res = await db.execute(
        update(ComputerDispatch)
        .where(ComputerDispatch.device_id == device.id,
               ComputerDispatch.status == "pending")
        .values(status="expired", error="设备已吊销", finished_at=_now()))
    await db.commit()
    logger.info("远程设备已吊销 device=%s 取消命令=%s", device.id, res.rowcount)
    return int(res.rowcount or 0)


async def list_devices(db: AsyncSession, company_id: str) -> list[ComputerDevice]:
    rows = (await db.execute(
        select(ComputerDevice).where(ComputerDevice.company_id == company_id)
        .order_by(ComputerDevice.created_at))).scalars().all()
    return list(rows)


async def get_device(db: AsyncSession, company_id: str,
                     device_id: str) -> ComputerDevice | None:
    return (await db.execute(
        select(ComputerDevice).where(ComputerDevice.id == device_id,
                                     ComputerDevice.company_id == company_id)
    )).scalars().first()


def device_out(device: ComputerDevice, now: datetime | None = None) -> dict:
    """给前端的设备视图。在线状态是推导出来的，不入库。"""
    return {
        "id": device.id,
        "name": device.name,
        "platform": device.platform,
        "hostname": device.hostname,
        "agent_version": device.agent_version,
        "capabilities": list(device.capabilities or []),
        "enrolled": device.enrolled_at is not None,
        "enrollment_ready": device.has_enrollment,
        "enrollment_expires_at": (device.enroll_expires_at.isoformat()
                                  if device.enroll_expires_at else None),
        "online": device.is_online(now),
        "last_seen_at": (device.last_seen_at.isoformat()
                         if device.last_seen_at else None),
        "revoked": bool(device.revoked),
    }


# ---------------------------------------------------------------- 设备侧


async def enroll(db: AsyncSession, *, code: str, name: str | None = None,
                 platform: str | None = None, hostname: str | None = None,
                 agent_version: str | None = None,
                 capabilities: list[str]) -> tuple[ComputerDevice, str]:
    """接入码换设备密钥。用后即焚：成功后接入码当场作废。

    同一个接入码重复使用 → 找不到（哈希已被清掉），返回 404，
    设备端应提示「回老板那边重新生成接入码」而不是重试。
    """
    h = _hash(code or "")
    device = (await db.execute(
        select(ComputerDevice).where(ComputerDevice.enroll_code_hash == h)
    )).scalars().first()
    if device is None:
        raise LookupError("接入码无效或已被使用")
    if device.revoked:
        raise PermissionError("设备已吊销")
    if device.enroll_expires_at is None or device.enroll_expires_at <= _now():
        raise TimeoutError("接入码已过期，请在设备控制台重新生成")

    secret = secrets.token_urlsafe(32)
    device.secret_hash = _hash(secret)
    device.enroll_code_hash = None
    device.enroll_expires_at = None
    device.enrolled_at = _now()
    device.last_seen_at = _now()
    _apply_device_facts(device, platform=platform, hostname=hostname,
                        agent_version=agent_version, capabilities=capabilities,
                        name=name)
    await db.commit()
    logger.info("设备已接入 company=%s device=%s name=%s caps=%s",
                device.company_id, device.id, device.name, len(capabilities))
    return device, secret


def _apply_device_facts(device: ComputerDevice, *, platform: str | None,
                        hostname: str | None, agent_version: str | None,
                        capabilities: list[str] | None,
                        name: str | None = None) -> None:
    if name:
        device.name = name[:120]
    if platform:
        device.platform = platform[:40]
    if hostname:
        device.hostname = hostname[:120]
    if agent_version:
        device.agent_version = agent_version[:40]
    if capabilities is not None:
        # 只收已知能力名：设备虚报一个不存在的名字，等于给自己开了一条
        # 后端策略里没有的动作类型
        from app.computer_real import EXTENDED_CAPABILITIES

        device.capabilities = [c for c in capabilities if c in EXTENDED_CAPABILITIES]


async def device_by_secret(db: AsyncSession,
                           secret: str) -> ComputerDevice | None:
    if not secret:
        return None
    return (await db.execute(
        select(ComputerDevice).where(ComputerDevice.secret_hash == _hash(secret),
                                     ComputerDevice.revoked.is_(False))
    )).scalars().first()


async def heartbeat(db: AsyncSession, device: ComputerDevice, *,
                    platform: str | None = None, hostname: str | None = None,
                    agent_version: str | None = None,
                    capabilities: list[str] | None = None,
                    name: str | None = None) -> dict:
    """刷心跳 + 顺带把设备自述的事实更新一遍。"""
    device.last_seen_at = _now()
    _apply_device_facts(device, platform=platform, hostname=hostname,
                        agent_version=agent_version, capabilities=capabilities,
                        name=name)
    await db.commit()
    return {"online": True, "server_time": device.last_seen_at.isoformat(),
            "capabilities": list(device.capabilities or [])}


async def claim_one(db: AsyncSession, device: ComputerDevice) -> dict | None:
    """原子领取一条待执行命令。

    为什么先 UPDATE 再 SELECT：`WHERE status='pending'` 的行数只可能是 1，
    抢到的那个连接才看得到它。两个 sidecar 用同一台设备的密钥同时轮询时，
    同一条命令不会执行两遍（执行两遍 = 文件覆盖两次 / 命令跑两次）。
    """
    now = _now()
    pending = (await db.execute(
        select(ComputerDispatch.id).where(
            ComputerDispatch.device_id == device.id,
            ComputerDispatch.status == "pending",
            ComputerDispatch.deadline_at > now).limit(5)
    )).scalars().all()
    for dispatch_id in pending:
        res = await db.execute(
            update(ComputerDispatch)
            .where(ComputerDispatch.id == dispatch_id,
                   ComputerDispatch.status == "pending")
            .values(status="claimed", claimed_at=now))
        if not res.rowcount:
            await db.rollback()
            continue  # 被别的 sidecar 抢先了
        row = (await db.execute(
            select(ComputerDispatch).where(ComputerDispatch.id == dispatch_id)
        )).scalars().first()
        device.last_seen_at = now
        await db.commit()
        if row is None:
            return None
        return {"dispatch_id": row.id, "capability": row.capability,
                "params": row.params or {}, "agent_id": row.agent_id,
                "deadline_at": row.deadline_at.isoformat()}
    device.last_seen_at = now
    await db.commit()
    return None


async def claim_with_wait(db: AsyncSession, device: ComputerDevice,
                          wait: float = CLAIM_HOLD_SECONDS) -> dict | None:
    """长轮询领命令：最多替设备等 `wait` 秒。

    每一轮都要结束读事务再查——SQLite WAL 下一个读事务看到的是开始那一刻的快照，
    不 commit/rollback 就永远看不到别的连接刚插进来的命令。
    """
    deadline = asyncio.get_running_loop().time() + max(0.0, wait)
    while True:
        claimed = await claim_one(db, device)
        if claimed is not None:
            return claimed
        await db.rollback()  # 放开读快照（claim_one 已经 commit 过，这里兜底）
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL)


async def report(db: AsyncSession, device: ComputerDevice, dispatch_id: str, *,
                 ok: bool, result: dict | None = None,
                 error: str | None = None) -> dict:
    """设备回报结果。只认这台设备自己领到的、且还没终态的命令。

    重复回报（网络重试）返回当前状态而不是报错——sidecar 需要幂等。
    """
    row = (await db.execute(
        select(ComputerDispatch).where(ComputerDispatch.id == dispatch_id,
                                       ComputerDispatch.device_id == device.id)
    )).scalars().first()
    if row is None:
        raise LookupError("这条命令不属于该设备（或已被清理）")
    if row.terminal:
        return {"dispatch_id": row.id, "status": row.status, "accepted": False,
                "reason": "已是终态，重复回报已忽略"}
    row.status = "succeeded" if ok else "failed"
    row.result = result
    row.error = None if ok else (error or "设备执行失败")
    row.finished_at = _now()
    device.last_seen_at = row.finished_at
    await db.commit()
    logger.info("设备回报 device=%s dispatch=%s status=%s",
                device.id, row.id, row.status)
    return {"dispatch_id": row.id, "status": row.status, "accepted": True}


# ---------------------------------------------------------------- 下发方


async def dispatch(db: AsyncSession, *, device: ComputerDevice, company_id: str,
                   capability: str, params: dict,
                   agent_id: str | None = None, log_id: str | None = None,
                   timeout: timedelta | None = None) -> ComputerDispatch:
    """落一条待执行命令。timeout 会被夹在 5 秒 ~ MAX_DISPATCH_TIMEOUT 之间。

    默认值在调用时读模块常量（而不是写在参数签名里），测试才能把它改短——
    否则"设备没回报"那条分支要真的等两分钟才能测到。
    """
    timeout = min(max(MIN_DISPATCH_TIMEOUT,
                      timeout or DEFAULT_DISPATCH_TIMEOUT), MAX_DISPATCH_TIMEOUT)
    row = ComputerDispatch(
        id=str(uuid.uuid4()), company_id=company_id, device_id=device.id,
        capability=capability, params=params or {}, agent_id=agent_id,
        log_id=log_id, status="pending", deadline_at=_now() + timeout)
    db.add(row)
    await db.commit()
    logger.info("命令已下发 device=%s capability=%s dispatch=%s",
                device.id, capability, row.id)
    return row


async def await_result(dispatch_id: str, *,
                       grace_seconds: float | None = None) -> dict:
    """等一条命令到终态，返回执行器口径的结果 dict。

    等待上限就是这条命令自己的 `deadline_at`（下发时定的），再加一点宽限
    ——「结果正好在超时线上回报」不该被判成没回报。轮询用**独立短会话**：
    WAL 下一个读事务只看开始那一刻的快照，复用调用方的会话会一直看不到
    设备那边提交的结果；自己开短事务每轮重新读，才既正确又不替调用方
    commit/回滚它手里的改动。

    超时不写成"失败"：判 expired 后如实返回「设备未回报」，让调用方和审计
    都看得见真实状态，而不是把没结果粉饰成失败或成功。
    """
    from app.db.session import AsyncSessionLocal

    grace = RESULT_GRACE_SECONDS if grace_seconds is None else grace_seconds
    while True:
        async with AsyncSessionLocal() as sdb:
            row = (await sdb.execute(
                select(ComputerDispatch).where(ComputerDispatch.id == dispatch_id)
            )).scalars().first()
            if row is None:
                return {"ok": False, "error": "下发记录不存在"}
            if row.terminal:
                return _terminal_result(row)
            past_due = (_now() - _naive(row.deadline_at)).total_seconds() > grace
            if past_due:
                res = await sdb.execute(
                    update(ComputerDispatch)
                    .where(ComputerDispatch.id == dispatch_id,
                           ComputerDispatch.status.in_(("pending", "claimed")))
                    .values(status="expired", error="等待设备回报超时",
                            finished_at=_now()))
                await sdb.commit()
                logger.warning("下发未回报，判过期 dispatch=%s 影响行数=%s",
                               dispatch_id, res.rowcount)
                return {"ok": False, "expired": True, "remote": True,
                        "error": "设备在期限内没有回报结果（可能已离线，"
                                 "或卡在系统权限弹窗上）"}
        await asyncio.sleep(POLL_INTERVAL)


def _naive(value: datetime) -> datetime:
    """库里的 DateTime 列可能是 naive（SQLite）或 aware（PG + timezone）；
    统一压成 naive UTC 才能和 `_now()` 直接相减（混用时区会算出差 8 小时的
    「过去」，把还没到点的命令当场判成过期）。"""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _terminal_result(row: ComputerDispatch) -> dict:
    if row.status == "succeeded":
        result = dict(row.result or {})
        result.setdefault("ok", True)
        result["remote"] = True
        result["dispatch_id"] = row.id
        return result
    if row.status == "failed":
        result = dict(row.result or {})
        result["ok"] = False
        result["remote"] = True
        result["dispatch_id"] = row.id
        result.setdefault("error", row.error or "设备执行失败")
        return result
    return {"ok": False, "expired": True, "remote": True, "dispatch_id": row.id,
            "error": row.error or "设备未回报结果"}


async def recent_dispatches(db: AsyncSession, company_id: str,
                            *, limit: int = 20) -> list[dict]:
    rows = (await db.execute(
        select(ComputerDispatch).where(
            ComputerDispatch.company_id == company_id)
        .order_by(ComputerDispatch.created_at.desc()).limit(limit)
    )).scalars().all()
    return [{"id": r.id, "device_id": r.device_id, "capability": r.capability,
             "status": r.status, "error": r.error,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in rows]
