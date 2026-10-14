"""自然语言 → 电脑操作意图层。

老板不需要懂代码：「帮我打开微信」「整理一下桌面文件」直接执行。
"""
import asyncio
import re
from pathlib import Path

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


async def organize_folder(path: Path, dry_run: bool = False) -> dict:
    """按类型整理文件夹（图片/文档/表格/…子文件夹）。"""
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
        dest_dir = path / category
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


async def execute_nl(intent: dict) -> dict:
    """直接执行识别出的意图。"""
    action = intent["action"]
    if action == "open_app":
        proc = await asyncio.create_subprocess_exec(
            "open", "-a", intent["params"]["app"],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0
        return {"ok": ok,
                "message": f"✅ 已打开 {intent['params']['app']}" if ok
                else f"❌ 打不开 {intent['params']['app']}: {err.decode()[:100]}"}
    elif action == "organize_folder":
        # 先预览再实际执行
        preview = await organize_folder(Path(intent["params"]["path"]), dry_run=True)
        result = await organize_folder(Path(intent["params"]["path"]), dry_run=False)
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
