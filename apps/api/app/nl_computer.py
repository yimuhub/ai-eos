"""自然语言 → 电脑操作意图层。

老板不需要懂代码：「帮我打开微信」「整理一下桌面文件」直接执行。

## 安全边界（P0-4）

改造前 `execute_nl(intent)` 没有任何租户/授权校验：任何一条聊天消息
只要命中意图，就在服务器主机上执行 `open` / 移动文件。这等于给
LLM 和任何能发消息的人开了一条绕开 ComputerPermission 的通道。

现在的约束：
1. `company_id` 必填——执行前必须校验该公司的计算机能力授权；
2. 文件操作被限制在 `settings.storage_dir` 沙箱根之内；
3. 禁止通过自然语言打开终端/Shell 宿主（Terminal/iTerm/…），
   否则后续 type_text 就等于主机命令执行。
"""
import asyncio
import re
import uuid
from pathlib import Path

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 常用应用名称映射（中文名 → macOS 应用名/Bundle）
APP_MAP = {
    "微信": "WeChat", "wechat": "WeChat",
    "qq": "QQ",
    "钉钉": "DingTalk",
    "飞书": "Lark",
    "safari": "Safari", "浏览器": "Safari",
    "chrome": "Google Chrome", "谷歌浏览器": "Google Chrome",
    "edge": "Microsoft Edge",
    "备忘录": "Notes",
    "提醒事项": "Reminders",
    "日历": "Calendar",
    "邮件": "Mail",
    "计算器": "Calculator",
    "访达": "Finder", "finder": "Finder",
    "终端": "Terminal",
    "vscode": "Visual Studio Code", "代码编辑器": "Visual Studio Code",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "ppt": "Microsoft PowerPoint", "pptx": "Microsoft PowerPoint",
    "keynote": "Keynote",
    "音乐": "Music",
    "照片": "Photos",
    "设置": "System Settings", "系统设置": "System Settings",
}

# 禁止通过自然语言打开的 shell 宿主。`open -a Terminal` 之后，
# 任何 type_text / key 都等价于在主机上执行命令，可绕过 shell 白名单。
SHELL_HOST_APPS = frozenset({
    "terminal", "iterm", "iterm2", "console", "gnome-terminal",
    "konsole", "xterm", "alacritty", "kitty", "warp", "wezterm",
    "hyper", "tabby", "ghostty", "rio", "tmux",
})


def _normalize_app_name(name: str) -> str:
    n = str(name or "").strip().strip("'\"").lower()
    if n.endswith(".app"):
        n = n[:-4]
    return Path(n).name


def is_shell_host_app(name: str) -> bool:
    """应用名是否属于终端/Shell 宿主。"""
    return _normalize_app_name(name) in SHELL_HOST_APPS


# 文件分类规则（后缀 → 中文分类名 + 目标文件夹）
FILE_CATEGORIES = {
    "图片": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".svg", ".tiff"},
    "文档": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".pages"},
    "表格": {".xls", ".xlsx", ".csv", ".numbers"},
    "演示文稿": {".ppt", ".pptx", ".key"},
    "视频": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm"},
    "音频": {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"},
    "压缩包": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "安装包": {".dmg", ".pkg", ".app"},
    "代码": {".py", ".js", ".ts", ".html", ".css", ".json", ".sh", ".go", ".rs", ".java", ".c", ".cpp"},
}


def detect_intent(text: str) -> dict | None:
    """识别自然语言意图。返回 {action, params, description} 或 None。"""
    t = text.strip().lower()

    # ---------- 意图1：打开应用 ----------
    # 匹配「打开XX」
    m = re.search(r"(?:帮我\s*)?(?:打开|启动|运行|开一下|开启)\s*(.+?)(?:[。.！!？?\s]|$)", t)
    if m:
        app_raw = m.group(1).strip()
        # 尝试映射
        for key, app in APP_MAP.items():
            if key in app_raw:
                return {"action": "open_app", "params": {"app": app},
                        "description": f"打开 {app}"}
        # 直接用原文作为应用名（可能是英文名）
        if app_raw and len(app_raw) < 40:
            return {"action": "open_app", "params": {"app": app_raw},
                    "description": f"打开 {app_raw}"}

    # ---------- 意图2：整理文件夹 ----------
    # 「整理桌面」「整理下载」「清理桌面」等
    if any(kw in t for kw in ("整理", "清理", "收拾", "归档", "分类")):
        # 直接从整句里找位置词
        target_raw = ""
        for kw in ("下载文件夹", "桌面文件夹", "文档文件夹", "下载", "桌面", "文档",
                   "downloads", "desktop", "documents"):
            if kw in t:
                target_raw = kw
                break
        folder = _resolve_folder(target_raw)
        if folder:
            return {"action": "organize_folder", "params": {"path": str(folder)},
                    "description": f"整理 {folder.name} 文件夹"}

    return None


def _resolve_folder(raw: str) -> Path | None:
    """把口语中的位置词解析为真实路径。"""
    home = Path.home()
    raw = raw.lower()
    # 顺序很重要：先匹配更具体的词
    if "下载" in raw or "download" in raw:
        p = home / "Downloads"
    elif "文档" in raw or "document" in raw:
        p = home / "Documents"
    elif not raw or "桌面" in raw or "desktop" in raw:
        p = home / "Desktop"
    elif "文档" in raw or "document" in raw:
        p = home / "Documents"
    else:
        return None
    return p if p.exists() else None


def storage_root() -> Path:
    """自然语言文件操作的唯一沙箱根。

    与 core/sandbox.py 的解析基准保持一致：相对路径相对仓库根，
    最后 resolve() 消解 `..` 与符号链接。
    """
    from app.core.config import settings
    from app.core.sandbox import PROJECT_ROOT

    root = Path(settings.storage_dir or "storage")
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve(strict=False)


def resolve_organize_target(raw: str | Path) -> Path:
    """把待整理路径限制在 storage 沙箱根之内，越界抛 SandboxViolation。"""
    from app.core.sandbox import SandboxViolation

    root = storage_root()
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise SandboxViolation(
            f"整理路径必须在沙箱根 {root} 之内，拒绝访问：{resolved}",
            path=str(resolved))
    return resolved


async def organize_folder(path: Path, dry_run: bool = False) -> dict:
    """按类型整理文件夹（图片/文档/表格/…子文件夹）。仅限 storage 沙箱内。"""
    path = resolve_organize_target(path)
    if not path.exists():
        return {"ok": False, "error": f"路径不存在: {path}"}
    moved, skipped = [], []
    for f in path.iterdir():
        if f.is_dir() or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        category = None
        for cat, exts in FILE_CATEGORIES.items():
            if ext in exts:
                category = cat
                break
        if not category:
            skipped.append(f.name)
            continue
        # 分类子目录同样过沙箱：防止有人预先在 storage 里放一个指向
        # 外部的符号链接目录，把文件「整理」到沙箱之外。
        dest_dir = resolve_organize_target(path / category)
        if dry_run:
            moved.append({"file": f.name, "category": category,
                          "to": str(dest_dir / f.name)})
            continue
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f.name
        # 重名处理
        n = 1
        while dest.exists():
            stem, suffix = f.stem, f.suffix
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
        await asyncio.to_thread(f.rename, dest)
        moved.append({"file": f.name, "category": category,
                      "to": str(dest.relative_to(path))})
    return {"ok": True, "moved": moved, "skipped_count": len(skipped),
            "dry_run": dry_run}


# 自然语言动作 → 需要公司显式授权的计算机能力（默认拒绝）
ACTION_CAPABILITY = {
    "open_app": "run_app",
    "organize_folder": "run_write",
}


async def _require_computer_permission(company_id: str, capability: str,
                                       agent_id: str | None = None) -> None:
    """校验该公司的 computer 权限；未授权先落审计再抛 ComputerActionDenied。

    复用 services/computer_service.py 的 `is_capability_granted`，
    与 REST / 工具调用走同一套授权数据，不另造一份判断。
    """
    from app.computer_agent import ComputerActionLog
    from app.db.session import AsyncSessionLocal
    from app.services.computer_service import (
        ComputerActionDenied,
        is_capability_granted,
    )

    if not company_id:
        raise ValueError("company_id is required")

    async with AsyncSessionLocal() as db:
        if await is_capability_granted(db, company_id, capability):
            return
        reason = f"能力 '{capability}' 未获授权（默认拒绝）"
        db.add(ComputerActionLog(
            id=str(uuid.uuid4()), company_id=company_id, agent_id=agent_id,
            action_type=capability, allowed=False, deny_reason=reason))
        await db.commit()
    raise ComputerActionDenied(capability, reason)


async def execute_nl(intent: dict, company_id: str,
                     agent_id: str | None = None) -> dict:
    """执行识别出的意图。

    `company_id` 必填（位置或关键字均可）：没有租户上下文就不执行，
    调用方不能再用「默认公司」之类的兜底绕过授权。
    """
    from app.core.sandbox import SandboxViolation
    from app.services.computer_service import ComputerActionDenied

    action = (intent or {}).get("action")
    capability = ACTION_CAPABILITY.get(action)
    if not capability:
        return {"ok": False, "error": f"未知操作: {action}"}

    try:
        await _require_computer_permission(company_id, capability, agent_id)
    except ComputerActionDenied as exc:
        return {"ok": False, "denied": True, "error": exc.reason,
                "message": f"❌ 未授权：{exc.reason}"}

    if action == "open_app":
        app = str((intent.get("params") or {}).get("app", "")).strip()
        if not app or any(c in app for c in ";|&$`\n\r"):
            return {"ok": False, "error": "非法的应用名",
                    "message": "❌ 应用名不合法，已拒绝"}
        if is_shell_host_app(app):
            msg = (f"❌ 出于安全考虑，不能通过自然语言打开终端类应用（{app}）。"
                   "终端里的键入等同于主机命令执行，请手动打开。")
            logger.warning("NL open_app 被拒：company=%s agent=%s app=%r",
                           company_id, agent_id, app)
            return {"ok": False, "denied": True, "error": msg, "message": msg}
        proc = await asyncio.create_subprocess_exec(
            "open", "-a", app,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0
        return {"ok": ok,
                "message": f"✅ 已打开 {app}" if ok
                else f"❌ 打不开 {app}: {err.decode()[:100]}"}
    elif action == "organize_folder":
        raw_path = (intent.get("params") or {}).get("path")
        if not raw_path:
            return {"ok": False, "error": "缺少目标路径",
                    "message": "❌ 缺少要整理的目录路径"}
        try:
            target = resolve_organize_target(raw_path)
        except SandboxViolation as exc:
            logger.warning("NL organize_folder 越界：company=%s path=%r err=%s",
                           company_id, raw_path, exc)
            return {"ok": False, "denied": True, "error": str(exc),
                    "message": f"❌ 拒绝整理：{exc}"}
        # 先预览再实际执行
        await organize_folder(target, dry_run=True)
        result = await organize_folder(target, dry_run=False)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error"),
                    "message": f"❌ 整理失败：{result.get('error')}"}
        moved_by_cat: dict[str, int] = {}
        for m2 in result.get("moved", []):
            moved_by_cat[m2["category"]] = moved_by_cat.get(m2["category"], 0) + 1
        summary = "、".join(f"{k}×{v}" for k, v in moved_by_cat.items())
        msg = f"✅ 整理完成！共移动 {len(result.get('moved', []))} 个文件"
        if summary:
            msg += f"（{summary}）"
        if result.get("skipped_count"):
            msg += f"，跳过 {result['skipped_count']} 个未识别类型"
        return {"ok": True, "message": msg, "detail": result.get("moved", [])[:10]}
    return {"ok": False, "error": f"未知操作: {action}"}
