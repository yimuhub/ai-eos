"""设备授权与配对码（V5.4 局域网共享 · 手机扫码免密接入）

流程（学 Home Assistant 的配对模型）：
  1. 老板在桌面端「局域网共享」页点「📱 分享给我手机」
     → POST /pairings 生成一次性配对码（明文只返回这一次，库里存 SHA-256）
  2. 手机扫二维码 → GET /pair?token=***（公开端点，是接入入口）
     → 校验（未用 + 未过期）→ 登记设备（UA 名 + 指纹）→ 签发 token 对
       （refresh 带 device_id，进 refresh_tokens 库）→ 免密登录完成
  3. 老板在「已接入设备」里可吊销 → POST /devices/{id}/revoke
     → devices.revoked + 该设备全部 refresh_tokens revoked；
       短期 access JWT 自然过期（不追杀无状态 token）。

安全边界：配对码只用来"开登录"，开出的会话与账号密码登录同级——
写接口照旧全鉴权。10 分钟 TTL + 单次使用 + 库内只存哈希。
"""
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import issue_token_pair
from app.db.session import get_db
from app.models.device import Device, PairingToken
from app.models.refresh_token import RefreshToken
from app.models.user import User

router = APIRouter(prefix="", tags=["pairing"])

PAIRING_TTL_MINUTES = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _device_name_from_ua(ua: str) -> str:
    """从 UA 提取人类可读设备名（"Safari on iPhone" 风格）。"""
    ua = (ua or "").lower()
    os_name = "unknown"
    for token, name in [("iphone", "iPhone"), ("ipad", "iPad"),
                         ("android", "Android"), ("macintosh", "Mac"),
                         ("windows", "Windows"), ("linux", "Linux")]:
        if token in ua:
            os_name = name
            break
    browser = "browser"
    for token, name in [("edg", "Edge"), ("chrome", "Chrome"),
                        ("safari", "Safari"), ("firefox", "Firefox")]:
        if token in ua:
            browser = name
            break
    return f"{browser} on {os_name}"


# ---------- 生成配对码（桌面端触发，鉴权） ----------

@router.post("/pairings")
async def create_pairing(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成一次性配对码。返回明文 token（仅此一次）+ 有效期。"""
    raw, row = PairingToken.new()
    row.user_id = user.id
    db.add(row)
    await db.commit()
    hint = "手机扫码后自动登录并登记为设备；将 token 拼入 站点/pair?token=*** 访问（10 分钟内一次性）"
    return {"token": raw, "ttl_seconds": PAIRING_TTL_MINUTES * 60, "hint": hint}


# ---------- 扫码换登录（公开：手机浏览器带着 token 进来） ----------

@router.get("/pair")
async def consume_pairing(
    token: str = "",
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """校验配对码 → 登记设备 → 签发 token 对（免密）。"""
    if not token:
        raise HTTPException(400, "缺少配对码")
    h = hashlib.sha256(token.encode()).hexdigest()
    row = (await db.execute(
        select(PairingToken).where(PairingToken.token_hash == h)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "配对码无效")
    if row.used_at:
        raise HTTPException(409, "配对码已使用（一次性）")
    if row.expires_at <= _utcnow():
        raise HTTPException(410, "配对码已过期，请在电脑端重新生成")

    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "用户不存在")

    ip = request.client.host if request and request.client else ""
    ua = request.headers.get("user-agent", "") if request else ""
    ua_name = _device_name_from_ua(ua)
    # 设备识别：同用户 × 同 UA 名 × 同 /24 网段 × 未吊销 → 复用（更新 last_seen）；
    # 否则开新设备。nonce 只进指纹（审计用），不进匹配条件。
    ip_prefix = ".".join(ip.split(".")[:3]) if ip and ":" not in ip else ip
    nonce = secrets.token_hex(8)
    fp = f"{ua[:120]}|{ip_prefix}|{nonce}"[:190]
    existing = (await db.execute(
        select(Device).where(
            Device.user_id == user.id, Device.revoked.is_(False),
            Device.name == ua_name, Device.fingerprint.like(f"%|{ip_prefix}|"))
    )).scalars().all()
    if existing:
        dev = existing[0]
        dev.last_seen_at = _utcnow()
    else:
        dev = Device(user_id=user.id, name=ua_name, fingerprint=fp,
                     source="pairing", last_seen_at=_utcnow())
        db.add(dev)
        await db.flush()

    pair = await issue_token_pair(db, user.id, device_id=dev.id)
    row.used_at = _utcnow()
    row.device_id = dev.id
    await db.commit()
    return {
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "device": {"id": dev.id, "name": dev.name},
        "user": {"id": user.id, "username": user.username,
                 "display_name": user.display_name},
    }


# ---------- 设备列表 / 吊销（老板视角，鉴权） ----------

@router.get("/devices")
async def list_devices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Device).where(Device.user_id == user.id)
        .order_by(Device.created_at.desc())
    )).scalars().all()
    active = {
        d for d in (await db.execute(
            select(RefreshToken.device_id).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False),
                RefreshToken.device_id.is_not(None), RefreshToken.expires_at > _utcnow())
        )).scalars().all() if d
    }
    return [
        {"id": d.id, "name": d.name, "source": d.source,
         "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
         "revoked": d.revoked, "has_active_session": d.id in active}
        for d in rows
    ]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """吊销设备：该设备全部 refresh 立即失效；短期 access JWT 自然过期。"""
    dev = (await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user.id)
    )).scalar_one_or_none()
    if dev is None:
        raise HTTPException(404, "设备不存在")
    if not dev.revoked:
        dev.revoked = True
        dev.revoked_at = _utcnow()
    res = await db.execute(
        update(RefreshToken).where(
            RefreshToken.device_id == device_id,
            RefreshToken.user_id == user.id,
            RefreshToken.revoked.is_(False),
        ).values(revoked=True)
    )
    await db.commit()
    return {"revoked": True, "sessions_revoked": res.rowcount}
