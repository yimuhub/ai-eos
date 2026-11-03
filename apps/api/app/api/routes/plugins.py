"""插件库 API：目录、引导检测、一键安装、健康检查、启停与撤销登记。

安装/卸载属于写操作，统一要求 `Capability.MANAGE`；所有入口先校验公司归属。
安装命令只来自目录里的固定包名，不接受前端传入任意参数。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.user import User
from app.schemas.responses_plugins import (PluginActionOut, PluginCatalogOut)
from app.services import plugin_service
from app.services.plugin_service import PluginError

router = APIRouter(prefix="/plugins", tags=["plugins"])


async def _owned_guard(db: AsyncSession, user_id: str, company_id: str) -> None:
    await assert_company_capability(db, user_id, company_id, Capability.MANAGE)


def _raise(exc: PluginError) -> None:
    raise HTTPException(exc.status_code, exc.message) from exc


@router.get("/catalog", response_model=PluginCatalogOut)
async def list_catalog(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _owned_guard(db, user.id, company_id)
    return await plugin_service.list_catalog(db, company_id)


@router.post("/{plugin_id}/detect", response_model=PluginActionOut)
async def detect_installed(
    plugin_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """引导安装后的登记：检测 PATH 里的可执行文件并记录版本。"""
    await _owned_guard(db, user.id, company_id)
    try:
        return await plugin_service.detect_and_register(
            db, company_id, plugin_id, user.id)
    except PluginError as exc:
        _raise(exc)


@router.post("/{plugin_id}/install", response_model=PluginActionOut)
async def install_plugin(
    plugin_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一键安装：npm 全局安装 → 检测可执行文件 → 登记。"""
    await _owned_guard(db, user.id, company_id)
    try:
        return await plugin_service.install_managed(
            db, company_id, plugin_id, user.id)
    except PluginError as exc:
        _raise(exc)


@router.post("/{plugin_id}/health", response_model=PluginActionOut)
async def check_health(
    plugin_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _owned_guard(db, user.id, company_id)
    try:
        return await plugin_service.check_health(
            db, company_id, plugin_id, user.id)
    except PluginError as exc:
        _raise(exc)


@router.patch("/{plugin_id}", response_model=PluginActionOut)
async def toggle_plugin(
    plugin_id: str,
    enabled: bool = Query(...),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _owned_guard(db, user.id, company_id)
    try:
        return await plugin_service.set_enabled(
            db, company_id, plugin_id, enabled, user.id)
    except PluginError as exc:
        _raise(exc)


@router.delete("/{plugin_id}", response_model=PluginActionOut)
async def unregister_plugin(
    plugin_id: str,
    company_id: str = Query(...),
    remove_package: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销登记；`remove_package=true` 只对托管安装执行 npm uninstall。"""
    await _owned_guard(db, user.id, company_id)
    try:
        return await plugin_service.unregister(
            db, company_id, plugin_id, user.id, remove_package)
    except PluginError as exc:
        _raise(exc)
