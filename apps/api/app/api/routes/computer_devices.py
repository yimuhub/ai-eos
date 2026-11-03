"""远程执行设备 API：老板登记设备，设备端 sidecar 领命令。

两条入口，两套鉴权，不能混：
* 老板侧（`/computer-devices*`）走登录态 + 公司归属校验，管理设备、看队列；
* 设备侧（`/computer-devices/enroll` 与 `/computer-devices/tunnel/*`）只认
  设备密钥请求头。接入码是一次性的，换完密钥就作废——所以泄露接入码
  最多让人抢跑那一台机器的**注册**，抢不到命令：命令只会派给老板
  在页面上选中/设为默认的那台设备，且每条都要过后端策略门。
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user
from app.db.session import get_db
from app.models.computer_device import ONLINE_WINDOW, ComputerDevice
from app.models.user import User
from app.schemas.responses_computer import (
    DeviceClaimOut,
    DeviceCreateOut,
    DeviceDefaultOut,
    DeviceDispatchListOut,
    DeviceEnrollOut,
    DeviceHeartbeatOut,
    DeviceListOut,
    DeviceReportOut,
    DeviceRevokeOut,
    DeviceRenewOut,
)
from app.services import computer_dispatch

router = APIRouter(prefix="/computer-devices", tags=["computer-devices"])

DEVICE_HEADER = "X-AIEOS-Device-Key"


# ---------------------------------------------------------------- 老板侧


class DeviceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


async def _owned_device(db: AsyncSession, user: User, company_id: str,
                        device_id: str) -> ComputerDevice:
    await assert_company_owned(db, user.id, company_id)
    device = await computer_dispatch.get_device(db, company_id, device_id)
    if device is None:
        # 别的公司的设备 id 也走到这里：不区分"不存在"和"不是你的"
        raise HTTPException(404, "设备不存在")
    return device


@router.get("", response_model=DeviceListOut)
async def list_devices(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """设备清单 + 公司默认执行机。在线状态按最近心跳推导。"""
    await assert_company_owned(db, user.id, company_id)
    from app.services import computer_policy as policy_service

    row = await policy_service.get_policy_row(db, company_id)
    devices = await computer_dispatch.list_devices(db, company_id)
    return {"devices": [computer_dispatch.device_out(d) for d in devices],
            "default_device_id": (row.default_device_id if row else None),
            "online_window_seconds": int(ONLINE_WINDOW.total_seconds())}


@router.post("", response_model=DeviceCreateOut)
async def create_device(
    data: DeviceCreateIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记一台设备，返回一次性接入码。设备上跑 sidecar 时把它贴进去。"""
    await assert_company_owned(db, user.id, company_id)
    name = data.name.strip()
    if any(d.name == name for d in await computer_dispatch.list_devices(
            db, company_id)):
        raise HTTPException(409, f"已有一台叫「{name}」的设备，换个名字或先吊销旧的")
    device, code = await computer_dispatch.create_device(
        db, company_id=company_id, name=name, created_by=user.id)
    return {"device": computer_dispatch.device_out(device), "enroll_code": code,
            "hint": "接入码 30 分钟内一次性有效；只显示这一次，丢了就重新生成"}


@router.post("/{device_id}/renew", response_model=DeviceRenewOut)
async def renew_enrollment(
    device_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """换一张新接入码（旧的当场作废）。已经接入过的设备也能换——
    换密钥 = 让老 sidecar 失效，这是「设备易主」唯一的收口办法。"""
    device = await _owned_device(db, user, company_id, device_id)
    code = await computer_dispatch.renew_enrollment(db, device)
    return {"enroll_code": code,
            "expires_at": device.enroll_expires_at.isoformat()}


@router.post("/{device_id}/revoke", response_model=DeviceRevokeOut)
async def revoke_device(
    device_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services import computer_policy as policy_service

    device = await _owned_device(db, user, company_id, device_id)
    cancelled = await computer_dispatch.revoke_device(db, device)
    # 默认执行机被吊销必须一起清掉：留着会让之后的每次操作都撞「设备已吊销」
    await policy_service.clear_default_device(db, company_id, device_id)
    return {"revoked": True, "cancelled_dispatches": cancelled}


@router.post("/{device_id}/set-default", response_model=DeviceDefaultOut)
async def set_default_device(
    device_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把这台设备设成公司默认执行机：聊天里的 computer_* 与未指定设备的
    REST 调用都会落到它上面（改造前是「永远在本机」）。"""
    device = await _owned_device(db, user, company_id, device_id)
    if not device.enrolled_at:
        raise HTTPException(409, "这台设备还没接入（sidecar 没跑过），先按接入码启动")
    if device.revoked:
        raise HTTPException(409, "设备已吊销，不能设为默认")
    from app.services import computer_policy as policy_service

    await policy_service.set_default_device(db, company_id=company_id,
                                            device_id=device.id,
                                            updated_by=user.id)
    return {"default_device_id": device.id, "name": device.name}


@router.post("/{device_id}/clear-default", response_model=DeviceDefaultOut)
async def clear_default_device(
    device_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消默认执行机（回到本机直连）。只有当前默认确实是这台时才生效。"""
    await _owned_device(db, user, company_id, device_id)
    from app.services import computer_policy as policy_service

    cleared = await policy_service.clear_default_device(
        db, company_id, device_id, updated_by=user.id)
    return {"default_device_id": None, "cleared": cleared}


@router.get("/dispatches", response_model=DeviceDispatchListOut)
async def list_dispatches(
    company_id: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await assert_company_owned(db, user.id, company_id)
    return {"dispatches": await computer_dispatch.recent_dispatches(
        db, company_id, limit=limit)}


# ---------------------------------------------------------------- 设备侧


class DeviceEnrollIn(BaseModel):
    code: str = Field(min_length=1, max_length=120)
    name: str | None = None
    platform: str | None = None
    hostname: str | None = None
    agent_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class DeviceHeartbeatIn(BaseModel):
    platform: str | None = None
    hostname: str | None = None
    agent_version: str | None = None
    capabilities: list[str] | None = None


class DeviceReportIn(BaseModel):
    dispatch_id: str = Field(min_length=1, max_length=36)
    ok: bool
    result: dict | None = None
    error: str | None = None


@router.post("/enroll", response_model=DeviceEnrollOut)
async def enroll_device(data: DeviceEnrollIn, db: AsyncSession = Depends(get_db)):
    """接入码换设备密钥。密钥只返回这一次，之后每次请求都带它。"""
    try:
        device, secret = await computer_dispatch.enroll(
            db, code=data.code, name=data.name, platform=data.platform,
            hostname=data.hostname, agent_version=data.agent_version,
            capabilities=data.capabilities)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(410, str(exc)) from exc
    return {"device_id": device.id, "secret": secret, "name": device.name,
            "capabilities": list(device.capabilities or [])}


async def _require_device(
    db: AsyncSession = Depends(get_db),
    device_key: str | None = Header(None, alias=DEVICE_HEADER),
) -> ComputerDevice:
    device = await computer_dispatch.device_by_secret(db, device_key or "")
    if device is None:
        raise HTTPException(401, "设备密钥无效或已吊销")
    return device


@router.post("/tunnel/heartbeat", response_model=DeviceHeartbeatOut)
async def tunnel_heartbeat(
    data: DeviceHeartbeatIn,
    device: ComputerDevice = Depends(_require_device),
    db: AsyncSession = Depends(get_db),
):
    out = await computer_dispatch.heartbeat(
        db, device, platform=data.platform, hostname=data.hostname,
        agent_version=data.agent_version, capabilities=data.capabilities)
    return out


@router.post("/tunnel/claim", response_model=DeviceClaimOut)
async def tunnel_claim(
    device: ComputerDevice = Depends(_require_device),
    db: AsyncSession = Depends(get_db),
    wait: float = Query(computer_dispatch.CLAIM_HOLD_SECONDS, ge=0, le=30),
):
    """领取一条待执行命令（服务端最多替设备等 `wait` 秒）。"""
    task = await computer_dispatch.claim_with_wait(db, device, wait=wait)
    return {"task": task}


@router.post("/tunnel/report", response_model=DeviceReportOut)
async def tunnel_report(
    data: DeviceReportIn,
    device: ComputerDevice = Depends(_require_device),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await computer_dispatch.report(
            db, device, data.dispatch_id, ok=data.ok, result=data.result,
            error=data.error)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
