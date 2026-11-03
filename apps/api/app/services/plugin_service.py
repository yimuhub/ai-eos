"""插件目录与安装登记。

插件包不经过本仓库分发：`agent-desktop` 走 npm 官方源（商业版不带），
其余条目只是目录占位。这里只做三件事——识别本机是否已安装、登记归属公司、
记录健康状态；不扫描磁盘、不执行任意路径。
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.plugin import PluginInstallation
from app.services.event_system import log_event

logger = get_logger(__name__)


class PluginError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


CATALOG: list[dict] = [
    {
        "id": "agent-desktop",
        "title": "agent-desktop",
        "vendor": "Vercel Labs",
        "version": "latest",
        "size": "—",
        "license": "Apache-2.0",
        "category": "桌面操作",
        "price": "free",
        "platforms": "macOS · Windows · Linux",
        "tagline": "直接读取界面结构，点击、输入、启动应用，不依赖截图。",
        "description": (
            "通过系统无障碍树读取界面元素与可操作引用（ref），再按引用点击、"
            "输入、滚动，不依赖截图与分辨率适配；默认 headless，不抢焦点。"
        ),
        "capabilities": ["界面快照", "点击与输入", "应用启动", "窗口管理", "无障碍树定位",
                         "文件读写与补丁"],
        "permissions": [
            {"name": "辅助功能", "required": True,
             "detail": "读取界面元素、模拟点击与键盘输入。macOS 需在「系统设置 → "
                       "隐私与安全性 → 辅助功能」中勾选授权。"},
            {"name": "屏幕录制", "required": False,
             "detail": "仅在没有无障碍信息的应用上作为回退；不授权也能使用绝大多数应用。"},
        ],
        "installable": True,
        "install": {"method": "npm", "package": "agent-desktop",
                    "binary": "agent-desktop"},
    },
    {
        "id": "git-workspace", "title": "git-workspace", "vendor": "AI-EOS",
        "version": "0.9.0", "size": "—", "license": "MIT", "category": "开发工具",
        "price": "free", "platforms": "macOS · Windows · Linux",
        "tagline": "本地仓库读写、补丁、测试与提交，改动可回溯、可撤销。",
        "description": "为开发类员工提供受控的仓库访问：读文件、生成 diff、跑测试、提交；"
                       "每次写入先备份，禁止自动 force push。",
        "capabilities": ["读取仓库", "生成补丁", "运行测试", "提交与分支"],
        "permissions": [{"name": "文件读写", "required": True,
                         "detail": "仅限已登记的仓库目录；每次写入前备份原文件。"}],
        "installable": False, "install": None,
    },
    {
        "id": "browser-runner", "title": "browser-runner", "vendor": "AI-EOS",
        "version": "1.1.0", "size": "—", "license": "商业许可", "category": "办公协作",
        "price": "paid", "platforms": "macOS · Windows · Linux",
        "tagline": "独立无头浏览器，批量完成网页任务、表单与截图存档。",
        "description": "与桌面操作隔离的浏览器运行时，适合登录态采集、表单填报与页面回归。",
        "capabilities": ["页面导航", "表单填写", "截图存档", "下载管理"],
        "permissions": [{"name": "网络访问", "required": True,
                         "detail": "访问任务指定的站点；可配置域名白名单。"}],
        "installable": False, "install": None,
    },
    {
        "id": "office-bridge", "title": "office-bridge", "vendor": "AI-EOS",
        "version": "0.6.0", "size": "—", "license": "商业许可", "category": "办公协作",
        "price": "paid", "platforms": "macOS · Windows",
        "tagline": "批量处理文档、表格与演示，按模板生成交付件。",
        "description": "把知识库内容与 Office 模板对接，批量生成报告、报价单与周报。",
        "capabilities": ["模板填充", "批量转换", "表格计算", "PDF 导出"],
        "permissions": [{"name": "文件读写", "required": True,
                         "detail": "仅限选定的输入与输出目录。"}],
        "installable": False, "install": None,
    },
    {
        "id": "sqlite-inspector", "title": "sqlite-inspector", "vendor": "AI-EOS",
        "version": "0.3.1", "size": "—", "license": "MIT", "category": "数据",
        "price": "free", "platforms": "macOS · Windows · Linux",
        "tagline": "只读浏览本地数据库结构与数据，帮助员工自查业务。",
        "description": "只读连接本地 SQLite 文件，展示表结构与样例数据。禁止任何写操作。",
        "capabilities": ["表结构浏览", "只读查询", "结果导出"],
        "permissions": [{"name": "文件读取", "required": True,
                         "detail": "只读打开登记过的数据库文件。"}],
        "installable": False, "install": None,
    },
]

_BY_ID = {item["id"]: item for item in CATALOG}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def catalog_entry(plugin_id: str) -> dict:
    spec = _BY_ID.get(plugin_id)
    if not spec:
        raise PluginError(f"未知插件 {plugin_id!r}", 404)
    return spec


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    tag = {"darwin": "darwin", "windows": "win32", "linux": "linux"}.get(
        system, system or "unknown")
    return f"{tag}-{machine}" if machine else tag


def _run(argv: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """固定 argv 调用，不经 shell；超时也视为失败。"""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def detect_plugin(plugin_id: str) -> dict:
    spec = catalog_entry(plugin_id)
    install = spec.get("install") or {}
    binary = install.get("binary") or plugin_id
    path = shutil.which(binary)
    if not path:
        return {"found": False, "binary": binary, "path": None,
                "version": None, "platform": _platform_tag(),
                "error": f"没有在 PATH 中找到 {binary}"}
    proc = _run([path, "--version"], timeout=10)
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    version = text[0][:80] if text else None
    return {
        "found": True,
        "binary": binary,
        "path": path,
        "version": version,
        "platform": _platform_tag(),
        "error": None if proc.returncode == 0 else (
            f"版本检测返回 {proc.returncode}：{(proc.stderr or '')[:200]}"),
    }


async def _installation(db: AsyncSession, company_id: str,
                        plugin_id: str) -> PluginInstallation | None:
    return (await db.execute(select(PluginInstallation).where(
        PluginInstallation.company_id == company_id,
        PluginInstallation.plugin_id == plugin_id))).scalars().first()


def serialize_installation(row: PluginInstallation | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row.id,
        "plugin_id": row.plugin_id,
        "version": row.version,
        "status": row.status,
        "source": row.source,
        "install_path": row.install_path,
        "executable": row.executable,
        "platform": row.platform,
        "enabled": row.enabled,
        "last_health_ok": row.last_health_ok,
        "last_health_at": row.last_health_at.isoformat() if row.last_health_at else None,
        "last_health_error": row.last_health_error,
        "config": row.config or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_catalog(db: AsyncSession, company_id: str) -> dict:
    rows = (await db.execute(select(PluginInstallation).where(
        PluginInstallation.company_id == company_id))).scalars().all()
    installed = {row.plugin_id: row for row in rows}
    items = []
    for spec in CATALOG:
        item = {k: v for k, v in spec.items() if k != "install"}
        item["installed"] = spec["id"] in installed
        item["installation"] = serialize_installation(installed.get(spec["id"]))
        items.append(item)
    return {
        "plugins": items,
        "installed_count": sum(1 for p in items if p["installed"]),
        "installable_count": sum(1 for p in items if p["installable"]),
    }


async def _upsert(db: AsyncSession, company_id: str, plugin_id: str,
                  *, source: str, detected: dict, user_id: str,
                  status: str = "installed") -> PluginInstallation:
    row = await _installation(db, company_id, plugin_id)
    if not row:
        row = PluginInstallation(company_id=company_id, plugin_id=plugin_id)
        db.add(row)
    row.version = detected.get("version")
    row.status = status
    row.source = source
    row.executable = detected.get("path")
    row.install_path = None
    row.platform = detected.get("platform")
    row.enabled = True
    row.last_health_ok = detected.get("found")
    row.last_health_at = _now()
    row.last_health_error = detected.get("error")
    row.installed_by = user_id
    await db.commit()
    await db.refresh(row)
    return row


async def detect_and_register(db: AsyncSession, company_id: str,
                              plugin_id: str, user_id: str) -> dict:
    """引导安装的落点：用户自己在终端装好后，这里检测并登记。"""
    detected = await asyncio.to_thread(detect_plugin, plugin_id)
    if not detected["found"]:
        raise PluginError(
            f"未检测到 {plugin_id}：{detected['error']}。"
            "请先在终端完成安装（npm i -g agent-desktop），再点一次检测。", 404)
    row = await _upsert(db, company_id, plugin_id, source="cli",
                        detected=detected, user_id=user_id)
    await log_event(db, event_type="plugin.detected", entity_type="plugin",
                    entity_id=plugin_id, actor_type="user", actor_id=user_id,
                    company_id=company_id,
                    payload={"version": row.version, "path": row.executable})
    await db.commit()
    return {"ok": True, "message": f"已登记 {plugin_id}", "installation":
            serialize_installation(row), "detected": detected}


async def install_managed(db: AsyncSession, company_id: str,
                          plugin_id: str, user_id: str) -> dict:
    spec = catalog_entry(plugin_id)
    install = spec.get("install") or {}
    if not spec.get("installable") or install.get("method") != "npm":
        raise PluginError(f"{plugin_id} 暂不提供一键安装，请等待上线", 400)

    npm = shutil.which("npm")
    if not npm:
        raise PluginError("没有找到 npm：请先安装 Node.js 18+，"
                          "或改用「引导安装」在终端手动安装。", 424)
    package = install["package"]
    logger.info("插件安装开始 plugin=%s package=%s", plugin_id, package)
    proc = await asyncio.to_thread(
        _run, [npm, "install", "-g", package], 300)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise PluginError(f"npm 安装失败（退出码 {proc.returncode}）：{detail}", 502)

    detected = await asyncio.to_thread(detect_plugin, plugin_id)
    if not detected["found"]:
        raise PluginError(
            "安装命令执行成功，但没有找到可执行文件。请确认 npm 全局 bin 目录在 PATH 中，"
            "然后点「检测已安装」。", 502)
    row = await _upsert(db, company_id, plugin_id, source="managed",
                        detected=detected, user_id=user_id)
    await log_event(db, event_type="plugin.installed", entity_type="plugin",
                    entity_id=plugin_id, actor_type="user", actor_id=user_id,
                    company_id=company_id,
                    payload={"version": row.version, "path": row.executable,
                             "source": "managed"})
    await db.commit()
    return {"ok": True, "message": f"{plugin_id} 已安装并登记",
            "installation": serialize_installation(row), "detected": detected}


async def check_health(db: AsyncSession, company_id: str, plugin_id: str,
                       user_id: str) -> dict:
    row = await _installation(db, company_id, plugin_id)
    if not row:
        raise PluginError("该插件还没有登记，先安装或检测", 404)
    detected = await asyncio.to_thread(detect_plugin, plugin_id)
    row.last_health_ok = detected["found"]
    row.last_health_at = _now()
    row.last_health_error = detected["error"]
    if detected["found"]:
        row.status = "installed" if row.enabled else "disabled"
        row.version = detected.get("version") or row.version
        row.executable = detected.get("path") or row.executable
    else:
        row.status = "error"
    await db.commit()
    await db.refresh(row)
    return {"ok": detected["found"], "message": "健康检查通过" if detected["found"]
            else f"健康检查失败：{detected['error']}",
            "installation": serialize_installation(row), "detected": detected}


async def set_enabled(db: AsyncSession, company_id: str, plugin_id: str,
                      enabled: bool, user_id: str) -> dict:
    row = await _installation(db, company_id, plugin_id)
    if not row:
        raise PluginError("该插件还没有登记，先安装或检测", 404)
    row.enabled = enabled
    row.status = "installed" if enabled else "disabled"
    row.installed_by = user_id
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "message": "已启用" if enabled else "已禁用",
            "installation": serialize_installation(row), "detected": None}


async def unregister(db: AsyncSession, company_id: str, plugin_id: str,
                     user_id: str, remove_package: bool = False) -> dict:
    """只撤销登记；`remove_package` 为真且是托管安装时才执行 npm uninstall。"""
    row = await _installation(db, company_id, plugin_id)
    if not row:
        raise PluginError("该插件还没有登记", 404)
    spec = _BY_ID.get(plugin_id) or {}
    if remove_package and row.source != "managed":
        raise PluginError("该插件由你自行安装，系统不代为卸载；"
                          "可以只撤销登记，或运行 npm uninstall -g 自行移除。", 400)
    if remove_package:
        npm = shutil.which("npm")
        package = (spec.get("install") or {}).get("package")
        if not npm or not package:
            raise PluginError("无法执行卸载：npm 或包名不可用", 424)
        proc = await asyncio.to_thread(
            _run, [npm, "uninstall", "-g", package], 120)
        if proc.returncode != 0:
            raise PluginError(
                f"npm 卸载失败：{(proc.stderr or '')[-300:]}", 502)
    await db.delete(row)
    await db.commit()
    await log_event(db, event_type="plugin.unregistered", entity_type="plugin",
                    entity_id=plugin_id, actor_type="user", actor_id=user_id,
                    company_id=company_id,
                    payload={"removed_package": remove_package})
    await db.commit()
    return {"ok": True, "message": "已撤销登记" if not remove_package
            else "已卸载并撤销登记", "installation": None, "detected": None}
