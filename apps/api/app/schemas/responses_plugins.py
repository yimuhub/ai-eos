"""插件库与本机操作策略的 API 响应模型。

与 `responses_computer.py` 的分工相同：按模块分文件，避免并行改动撞车。
可空字段按「数据库列 nullable + 当前真实分支」声明；执行器自由返回的
`detected` / `config` 保持 dict，避免钉死形状后把 200 变成 500。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


class PluginPermissionOut(BaseModel):
    model_config = _free()

    name: str
    required: bool
    detail: str


class PluginInstallationOut(BaseModel):
    model_config = _free()

    id: str
    plugin_id: str
    version: str | None = None
    status: str
    source: str
    install_path: str | None = None
    executable: str | None = None
    platform: str | None = None
    enabled: bool
    last_health_ok: bool | None = None
    last_health_at: str | None = None
    last_health_error: str | None = None
    config: dict | None = None
    created_at: str | None = None


class PluginCatalogItemOut(BaseModel):
    model_config = _free()

    id: str
    title: str
    vendor: str
    version: str
    size: str
    license: str
    category: str
    price: str
    platforms: str
    tagline: str
    description: str
    capabilities: list[str]
    permissions: list[PluginPermissionOut]
    installable: bool
    installed: bool
    installation: PluginInstallationOut | None = None


class PluginCatalogOut(BaseModel):
    model_config = _free()

    plugins: list[PluginCatalogItemOut]
    installed_count: int
    installable_count: int


class PluginActionOut(BaseModel):
    """检测 / 安装 / 健康检查 / 启停 / 卸载共用。"""

    model_config = _free()

    ok: bool
    message: str
    installation: PluginInstallationOut | None = None
    detected: dict | None = None


class ComputerPolicyOut(BaseModel):
    model_config = _free()

    id: str | None = None
    mode: str
    auto_dirs: list[str]
    auto_commands: list[str]
    auto_apps: list[str]
    full_control_until: str | None = None
    notify_on_finish: bool
    updated_by: str | None = None
    configured: bool


class HardBlockGroupOut(BaseModel):
    model_config = _free()

    kind: str
    label: str
    examples: list[str]
