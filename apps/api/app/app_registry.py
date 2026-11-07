"""跨平台应用注册表：扫描本机已装应用 + 自然语言别名映射。

任何人在本地部署 AI-EOS 后，系统自动识别操作系统类型：
- macOS:  扫描 /Applications 与 /System/Applications 下的 .app
- Windows: 扫描开始菜单 .lnk 快捷方式（含用户与全局两处）
- Linux:  解析 /usr/share/applications 与 ~/.local/share/applications 的 .desktop

聊天时自然语言 → 应用名匹配顺序：
1. 用户自定义别名规则（数据库 AppAliasRule，公司级）
2. 内置常用中文映射（微信→WeChat 等）
3. 本机扫描到的应用模糊匹配
"""
import os
import platform
from pathlib import Path

from app.core.logging_config import get_logger

logger = get_logger(__name__)


def get_platform() -> dict:
    """返回当前系统信息。"""
    sysname = platform.system()  # Darwin / Windows / Linux
    return {
        "os": {"Darwin": "mac", "Windows": "windows", "Linux": "linux"}.get(
            sysname, sysname.lower()),
        "os_name": platform.system(),
        "os_version": platform.mac_ver()[0] if sysname == "Darwin"
        else platform.release(),
        "machine": platform.machine(),
    }


def scan_apps() -> list[dict]:
    """扫描本机已安装应用，返回 [{name, path}]。按名称去重排序。"""
    p = get_platform()["os"]
    apps: list[dict] = []
    try:
        if p == "mac":
            apps = _scan_mac()
        elif p == "windows":
            apps = _scan_windows()
        elif p == "linux":
            apps = _scan_linux()
    except Exception as scan_err:
        # 扫描失败会静默表现为"本机一个应用都没有"，用户无从判断是环境问题
        logger.warning("扫描本机应用失败 platform=%s: %s", p, scan_err,
                       exc_info=True)
    # 去重（同名保留第一个）
    seen, out = set(), []
    for a in sorted(apps, key=lambda x: x["name"].lower()):
        key = a["name"].lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def _scan_mac() -> list[dict]:
    apps = []
    for base in ("/Applications", "/System/Applications",
                 str(Path.home() / "Applications")):
        bp = Path(base)
        if not bp.exists():
            continue
        for item in bp.iterdir():
            if item.suffix == ".app" and not item.name.startswith("."):
                apps.append({"name": item.stem,
                             "path": str(item),
                             "source": base})
    return apps


def _scan_windows() -> list[dict]:
    """Windows: 开始菜单 .lnk 快捷方式（免 win32 依赖，直接枚举文件）。"""
    apps = []
    bases = []
    pf = os.environ.get("ProgramData", r"C:\ProgramData")
    af = os.environ.get("AppData")
    for root in (pf, af):
        if root:
            bases.append(Path(root) / "Microsoft" / "Windows"
                         / "Start Menu" / "Programs")
    for base in bases:
        if not base.exists():
            continue
        for lnk in base.rglob("*.lnk"):
            name = lnk.stem
            if name.lower() in ("uninstall", "卸载"):
                continue
            apps.append({"name": name, "path": str(lnk), "source": str(base)})
    return apps


def _scan_linux() -> list[dict]:
    """Linux: 解析 .desktop 文件的 Name 字段。"""
    apps = []
    bases = [Path("/usr/share/applications"),
             Path.home() / ".local/share/applications"]
    for base in bases:
        if not base.exists():
            continue
        for d in base.glob("*.desktop"):
            name, hidden, terminal = d.stem, False, False
            try:
                for line in d.read_text(encoding="utf-8",
                                        errors="ignore").splitlines():
                    if line.startswith("Name="):
                        name = line.split("=", 1)[1].strip()
                    elif line.startswith("NoDisplay=true"):
                        hidden = True
                    elif line.startswith("Terminal=true"):
                        terminal = True
            except Exception:
                logger.debug("读不了 .desktop 文件，跳过该应用：%s", d, exc_info=True)
                continue
            if hidden or name.lower() in ("uninstall",):
                continue
            apps.append({"name": name, "path": str(d), "terminal": terminal,
                         "source": str(base)})
    return apps


def build_launch_command(path: str | None, app_name: str) -> tuple[list[str], bool]:
    """返回 (启动命令 argv, 是否 shell 免)。跨平台启动应用。

    mac:    open -a <AppName 或 .app 路径>
    windows: start "" <lnk/exe>   （经 cmd）
    linux:  gtk-launch <desktop id> 或直接执行 Exec
    """
    p = get_platform()["os"]
    if p == "mac":
        target = path or app_name
        return ["open", "-a", target], True
    if p == "windows":
        # cmd /c start "" "路径或名称"
        return ["cmd", "/c", "start", "", path or app_name], True
    # linux
    if path and path.endswith(".desktop"):
        desktop_id = Path(path).stem
        return ["gtk-launch", desktop_id], True
    if path:
        return ["xdg-open", path], True
    return ["xdg-open", app_name], True


# ---------------- 内置中文映射（通用，所有平台共享）----------------
BUILTIN_ALIASES = {
    "微信": "WeChat", "wechat": "WeChat", "qq": "QQ", "钉钉": "DingTalk",
    "飞书": "Lark", "safari": "Safari", "浏览器": "浏览器占位",
    "chrome": "Google Chrome", "谷歌浏览器": "Google Chrome",
    "edge": "Microsoft Edge", "备忘录": "Notes", "提醒事项": "Reminders",
    "日历": "Calendar", "邮件": "Mail", "计算器": "Calculator",
    "访达": "Finder", "finder": "Finder", "终端": "Terminal",
    "vscode": "Visual Studio Code", "代码编辑器": "Visual Studio Code",
    "word": "Microsoft Word", "excel": "Microsoft Excel",
    "ppt": "Microsoft PowerPoint", "keynote": "Keynote",
    "音乐": "Music", "照片": "Photos", "设置": "System Settings",
    "系统设置": "System Settings",
}


def match_app(text: str, custom_rules: list[dict] | None = None,
              installed: list[dict] | None = None) -> dict | None:
    """自然语言 → 应用匹配。优先级：自定义规则 > 内置映射 > 本机扫描模糊匹配。

    custom_rules: [{"alias": "vx", "app_name": "WeChat"}]
    installed:    [{"name": "WeChat", "path": "/Applications/WeChat.app"}]
    返回 {"name", "path", "via"} 或 None。
    """
    t = text.strip().lower()
    if not t:
        return None
    # 1. 自定义规则（大小写不敏感，最长别名优先）
    if custom_rules:
        for rule in sorted(custom_rules, key=lambda r: len(r.get("alias", "")),
                           reverse=True):
            alias = (rule.get("alias") or "").lower()
            if alias and alias in t:
                return {"name": rule.get("app_name"),
                        "path": rule.get("app_path"),
                        "via": f"自定义规则「{rule.get('alias')}」"}
    # 2. 本机扫描精确/包含匹配（先于内置映射，因为内置可能指向未安装的应用）
    if installed:
        for a in installed:
            if a["name"].lower() == t:
                return {"name": a["name"], "path": a["path"], "via": "本机应用"}
        for kw, std in BUILTIN_ALIASES.items():
            if kw in t:
                for a in installed:
                    if a["name"].lower() == std.lower():
                        return {"name": a["name"], "path": a["path"],
                                "via": f"内置映射「{kw}」"}
        # 模糊：应用名是输入的子串
        for a in installed:
            n = a["name"].lower()
            if len(n) >= 2 and n in t:
                return {"name": a["name"], "path": a["path"],
                        "via": "本机应用(包含匹配)"}
        # 内置映射兜底（应用未扫描到也给出名字，让 open 报错前有提示）
        for kw, std in BUILTIN_ALIASES.items():
            if kw in t and std != "浏览器占位":
                return {"name": std, "path": None, "via": f"内置映射「{kw}」"}
        return None
    # 3. 无扫描结果时用内置映射
    for kw, std in BUILTIN_ALIASES.items():
        if kw in t and std != "浏览器占位":
            return {"name": std, "path": None, "via": f"内置映射「{kw}」"}
    return None
