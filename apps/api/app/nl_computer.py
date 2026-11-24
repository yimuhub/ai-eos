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
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import suppress
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
    # 「整理计划」「清理思路」是业务表达，不是文件操作。只有动作词、明确的
    # 文件/目录对象、明确的位置或路径三者同时出现才进入整理分支。
    if any(kw in t for kw in ("整理", "清理", "收拾", "归档", "分类")):
        target_raw = _extract_organize_target(text.strip())
        if target_raw:
            # 识别层只保留逻辑目标；执行层再绑定公司并解析路径，绝不把
            # Detection 时的 Desktop/Downloads 展开成宿主目录。
            target = Path(target_raw)
            return {"action": "organize_folder", "params": {"path": target_raw},
                    "description": f"整理 {target.name or target} 文件夹"}

    return None


KNOWN_LOCATION_PATHS = ("Downloads", "Desktop", "Documents")


def _looks_like_url(raw: str) -> bool:
    """scheme/www/裸域名或 IP 形式的 URL；不把业务路径误判成网络地址。"""
    value = str(raw or "").strip().strip("'\"")
    if re.match(r"^(?:[a-z][a-z0-9+.-]*://|www\.)", value, re.IGNORECASE):
        return True
    host = re.split(r"[/\\]", value, maxsplit=1)[0]
    return bool(re.fullmatch(
        r"(?:(?:[a-z0-9-]+\.)+[a-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?",
        host, re.IGNORECASE))


def _looks_like_path(raw: str) -> bool:
    """显式 Unix/home/相对路径；URL、日期和单纯的位置词不算路径。"""
    value = str(raw or "").strip().strip("'\"")
    if not value or _looks_like_url(value):
        return False
    if re.fullmatch(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", value):
        return False
    return value.startswith(("/", "~/")) or "/" in value or "\\" in value


def _extract_organize_target(text: str) -> str | None:
    """提取明确的整理位置；宁可漏判，也不能把普通业务短语当成文件操作。"""
    raw_text = str(text or "").strip()
    t = raw_text.lower()
    file_object = any(kw in t for kw in
                      ("文件", "文件夹", "目录", "folder", "directory", "files"))
    location_words = (
        ("下载文件夹", "Downloads"), ("桌面文件夹", "Desktop"),
        ("文档文件夹", "Documents"), ("下载", "Downloads"),
        ("桌面", "Desktop"), ("文档", "Documents"),
        ("downloads", "Downloads"), ("desktop", "Desktop"),
        ("documents", "Documents"),
    )
    known_location = next((logical for raw, logical in location_words if raw in t), None)
    business_noun = any(kw in t for kw in
                        ("计划", "思路", "方案", "策略", "报告", "预算", "任务",
                         "项目", "会议", "需求", "清单"))

    # 支持带引号或不带引号的显式路径，例如「整理 storage/reports 文件夹」。
    # 不扫描整个句子找斜杠，避免把 URL 或日期误当路径。
    # 先把 URL 整体排除，避免从 scheme 后的主机名/路径里截出一个假相对路径。
    path_text = raw_text
    for path_match in re.finditer(
        r"\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s，。！？、；;]+",
        raw_text, flags=re.IGNORECASE):
        path_text = path_text.replace(path_match.group(0), " ", 1)
    for path_match in re.finditer(
        r"(?:~/(?:[^\s，。！？、；;]+)|/(?:[^\s，。！？、；;]+)|"
        r"(?<![\w:/])(?:[\w.-]+/)+[\w.-]+)", path_text):
        raw_path = path_match.group(0).rstrip(".,!?，。！？、；;)]}")
        # 日期即使出现在带“文件”二字的业务句子里也仍是业务意图。
        if (not raw_path or not _looks_like_path(raw_path)
                or re.fullmatch(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", raw_path)):
            continue
        if file_object or _looks_like_path(raw_path):
            return raw_path

    # 「桌面」「下载」「文档」本身可作为明确的口语目录名，但若句子里还有
    # 计划/思路等业务名词且没有文件对象，仍按业务表达处理。
    if known_location and (file_object or not business_noun):
        return known_location
    return None


def storage_root() -> Path:
    """公司整理沙箱的共享父根；公司根必须位于其 companies/<cid> 下。

    与 core/sandbox.py 的解析基准保持一致：相对路径相对仓库根，
    最后 resolve() 消解 `..` 与符号链接。
    """
    from app.core.config import settings
    from app.core.sandbox import PROJECT_ROOT

    root = Path(settings.storage_dir or "storage")
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve(strict=False)


def company_storage_root(company_id: str) -> Path:
    """每个公司独立的整理沙箱；绝不允许使用共享 storage 根本身。"""
    from app.core.sandbox import SandboxViolation

    cid = str(company_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid):
        raise SandboxViolation("非法公司标识，拒绝解析整理目录", path=cid)
    lexical_root = storage_root() / "companies" / cid
    root = lexical_root.resolve(strict=False)
    shared = storage_root().resolve(strict=False)
    lexical_companies = storage_root() / "companies"
    companies_root = lexical_companies.resolve(strict=False)
    if ((root != shared and shared not in root.parents)
            or companies_root != lexical_companies
            or root.parent != companies_root or root.name != cid):
        raise SandboxViolation("公司沙箱根被符号链接重定向到其它目录", path=str(root))
    return root


def resolve_organize_target(raw: str | Path, company_id: str) -> Path:
    """把待整理路径限制在指定公司的 storage 沙箱内，越界抛 SandboxViolation。

    文档中的 ``storage/reports`` 按仓库根解析为 ``PROJECT_ROOT/storage/reports``，
    不会变成 ``storage/storage/reports``；随后 rebase 到该公司的独立目录。
    其它仓库内相对路径（如 ``reports``）同样按公司目录解析。绝对路径必须
    已经位于本公司根内，不能借绝对路径访问别家公司或共享目录。
    """
    from app.core.sandbox import PROJECT_ROOT, SandboxViolation

    value = str(raw or "").strip()
    if not value:
        raise SandboxViolation("整理路径不能为空", path=value)
    if _looks_like_url(value):
        raise SandboxViolation("URL 不是整理路径", path=value)
    shared_root = storage_root().resolve(strict=False)
    root = company_storage_root(company_id)
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        repo_relative = (PROJECT_ROOT / candidate).resolve(strict=False)
        if repo_relative == shared_root or shared_root in repo_relative.parents:
            resolved = root / repo_relative.relative_to(shared_root)
        else:
            resolved = root / candidate
        resolved = resolved.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise SandboxViolation(
            f"整理路径必须在公司沙箱根 {root} 之内，拒绝访问：{resolved}",
            path=str(resolved))
    return resolved


def _organize_preview_hash(path: Path, moved: list[dict],
                          skipped_count: int, signatures: list[list[int]]) -> str:
    """预览指纹：路径、移动计划、源文件状态和跳过数量任一变化都会失效。"""
    payload = {"path": str(path), "moved": moved,
               "skipped_count": skipped_count, "source_signatures": signatures}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _open_company_root_fd(company_id: str) -> int:
    company_storage_root(company_id)
    shared_fd = os.open(storage_root(), _DIR_FLAGS)
    try:
        companies_fd = os.open("companies", _DIR_FLAGS, dir_fd=shared_fd)
        try:
            return os.open(company_id, _DIR_FLAGS, dir_fd=companies_fd)
        finally:
            os.close(companies_fd)
    finally:
        os.close(shared_fd)


def _open_relative_dir(root_fd: int, root: Path, target: Path) -> int:
    relative = target.relative_to(root)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise OSError("整理目录路径不合法")
    current_fd = os.dup(root_fd)
    try:
        for part in relative.parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _stat_signature(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns]


def _stat_content_signature(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _rename_noreplace(source_fd: int, source: str,
                      target_fd: int, target: str) -> None:
    """Atomically rename within verified directories without replacing a target."""
    import ctypes
    import errno
    import platform
    import sys

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "系统不支持原子禁止覆盖移动") from exc
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        flag = 4  # RENAME_EXCL
        syscall = None
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
            syscall = None
        except AttributeError:
            number = {
                "x86_64": 316, "amd64": 316, "aarch64": 276, "arm64": 276,
                "i386": 353, "i686": 353,
            }.get(platform.machine().lower())
            if number is None:
                raise OSError(errno.ENOTSUP, "系统不支持原子禁止覆盖移动")
            rename = libc.syscall
            rename.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            syscall = number
        if syscall is None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_uint]
        flag = 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "系统不支持原子禁止覆盖移动")
    rename.restype = ctypes.c_int
    args = (source_fd, os.fsencode(source), target_fd, os.fsencode(target))
    result = (rename(*args, flag) if syscall is None
              else rename(syscall, *args, flag))
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise OSError(errno.EEXIST, f"目标已存在，拒绝覆盖：{target}", target)
        raise OSError(error, os.strerror(error), target)


def _same_directory(fd: int, expected: Path) -> bool:
    actual = os.fstat(fd)
    try:
        current = os.stat(expected, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (stat.S_ISDIR(current.st_mode)
            and (actual.st_dev, actual.st_ino) == (current.st_dev, current.st_ino))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise OSError(f"整理路径在移动过程中被符号链接替换：{current}")


def _assert_fd_path(fd: int, expected: Path, label: str, *,
                    root_fd: int | None = None,
                    root: Path | None = None) -> None:
    if root_fd is not None and root is not None:
        checked_fd = _open_relative_dir(root_fd, root, expected)
        try:
            _assert_no_symlink_components(expected)
            retained = os.fstat(fd)
            checked = os.fstat(checked_fd)
            matches = (_same_directory(fd, expected)
                       and _same_directory(checked_fd, expected)
                       and (retained.st_dev, retained.st_ino)
                       == (checked.st_dev, checked.st_ino))
        finally:
            os.close(checked_fd)
    else:
        _assert_no_symlink_components(expected)
        matches = _same_directory(fd, expected)
    if not matches:
        raise OSError(f"{label}在移动过程中被移出公司沙箱：{expected}")


def _create_organize_backup(root_fd: int, target_fd: int,
                            source_fd: int, name: str) -> str:
    backup = f".aieos-organize-{uuid.uuid4().hex}"
    try:
        os.link(name, backup, src_dir_fd=target_fd, dst_dir_fd=root_fd,
                follow_symlinks=False)
        saved = os.stat(backup, dir_fd=root_fd, follow_symlinks=False)
        source = os.fstat(source_fd)
        if (not stat.S_ISREG(saved.st_mode)
                or (saved.st_dev, saved.st_ino) != (source.st_dev, source.st_ino)):
            raise OSError("整理安全副本身份不一致")
        return backup
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(backup, dir_fd=root_fd)
        raise


def _assert_organize_backup_stat(root_fd: int, source_stat: os.stat_result,
                                 backup: str) -> None:
    saved = os.stat(backup, dir_fd=root_fd, follow_symlinks=False)
    if (not stat.S_ISREG(saved.st_mode)
            or (saved.st_dev, saved.st_ino) != (source_stat.st_dev, source_stat.st_ino)):
        raise OSError("整理安全副本身份不一致")


def _assert_organize_backup(root_fd: int, source_fd: int, backup: str) -> None:
    _assert_organize_backup_stat(root_fd, os.fstat(source_fd), backup)


def _recover_organize_fd(root_fd: int, source_fd: int,
                         expected: list[int]) -> str:
    if _stat_content_signature(os.fstat(source_fd)) != expected[:4]:
        raise OSError("整理恢复前源文件身份不一致")
    recovery = f".aieos-organize-{uuid.uuid4().hex}"
    try:
        saved_fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                           0o600, dir_fd=root_fd)
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while chunk := os.read(source_fd, 1024 * 1024):
                os.write(saved_fd, chunk)
            os.fsync(saved_fd)
        finally:
            os.close(saved_fd)
        saved = os.stat(recovery, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(saved.st_mode) or saved.st_size != expected[2]:
            raise OSError("整理恢复文件内容不一致")
        return recovery
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(recovery, dir_fd=root_fd)
        raise


def _restore_organize_file(root_fd: int, category_fd: int, root: Path,
                           source_fd: int, backup: str | None, name: str,
                           expected: list[int]) -> str:
    _assert_fd_path(root_fd, root, "公司沙箱根")
    source = os.fstat(source_fd)
    try:
        moved = os.stat(name, dir_fd=category_fd, follow_symlinks=False)
    except FileNotFoundError:
        moved = None
    if moved is not None and stat.S_ISREG(moved.st_mode) \
            and (moved.st_dev, moved.st_ino) == (source.st_dev, source.st_ino) \
            and _stat_content_signature(moved) == expected[:4]:
        recovery = f".aieos-organize-{uuid.uuid4().hex}"
        _rename_noreplace(category_fd, name, root_fd, recovery)
        _assert_fd_path(root_fd, root, "公司沙箱根")
        recovered = os.stat(recovery, dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISREG(recovered.st_mode)
                or (recovered.st_dev, recovered.st_ino) != (source.st_dev, source.st_ino)
                or _stat_content_signature(recovered) != expected[:4]):
            raise OSError(f"整理恢复文件身份不一致：{recovery}")
        return recovery
    if backup is not None:
        try:
            _assert_organize_backup(root_fd, source_fd, backup)
        except OSError:
            backup = None
        else:
            _assert_fd_path(root_fd, root, "公司沙箱根")
            return backup
    return _recover_organize_fd(root_fd, source_fd, expected)


def _assert_organize_destination(root_fd: int, root: Path, target_fd: int,
                                 path: Path, category: str, category_fd: int,
                                 source_fd: int, name: str,
                                 expected: list[int]) -> None:
    _assert_fd_path(root_fd, root, "公司沙箱根")
    _assert_fd_path(target_fd, path, "整理目录", root_fd=root_fd, root=root)
    _assert_fd_path(category_fd, path / category, "分类目录",
                    root_fd=root_fd, root=root)
    dest_stat = os.stat(name, dir_fd=category_fd, follow_symlinks=False)
    if (not stat.S_ISREG(dest_stat.st_mode)
            or _stat_content_signature(dest_stat) != expected[:4]
            or dest_stat.st_ino != os.fstat(source_fd).st_ino):
        raise OSError(f"移动后的源文件身份不一致：{name}")


def _validate_organize_record(root_fd: int, root: Path, target_fd: int,
                              path: Path, record: dict) -> None:
    source_fd = os.open(record["name"], _FILE_FLAGS,
                        dir_fd=record["category_fd"])
    try:
        _assert_organize_destination(
            root_fd, root, target_fd, path, record["category"],
            record["category_fd"], source_fd, record["name"], record["expected"])
        if record["backup"] is not None:
            _assert_organize_backup(root_fd, source_fd, record["backup"])
    finally:
        os.close(source_fd)


def _cleanup_organize_record(root_fd: int, root: Path, target_fd: int,
                             path: Path, record: dict) -> None:
    source_fd = os.open(record["name"], _FILE_FLAGS,
                        dir_fd=record["category_fd"])
    try:
        _assert_organize_destination(
            root_fd, root, target_fd, path, record["category"],
            record["category_fd"], source_fd, record["name"], record["expected"])
        backup = record["backup"]
        if backup is None:
            raise OSError("整理安全副本缺失，拒绝完成")
        _assert_organize_backup(root_fd, source_fd, backup)
        try:
            os.unlink(backup, dir_fd=root_fd)
            record["backup"] = None
            _assert_organize_destination(
                root_fd, root, target_fd, path, record["category"],
                record["category_fd"], source_fd, record["name"], record["expected"])
        except BaseException:
            record["recovery"] = _restore_organize_file(
                root_fd, record["category_fd"], root, source_fd,
                record["backup"], record["name"], record["expected"])
            record["recovered"] = True
            raise
    finally:
        os.close(source_fd)


def _restore_organize_record(root_fd: int, root: Path, record: dict) -> str:
    _assert_fd_path(root_fd, root, "公司沙箱根")
    source = record["source_stat"]
    name = record["name"]
    try:
        moved = os.stat(name, dir_fd=record["category_fd"], follow_symlinks=False)
    except FileNotFoundError:
        moved = None
    if moved is not None and stat.S_ISREG(moved.st_mode) \
            and (moved.st_dev, moved.st_ino) == (source.st_dev, source.st_ino) \
            and _stat_content_signature(moved) == record["expected"][:4]:
        recovery = f".aieos-organize-{uuid.uuid4().hex}"
        _rename_noreplace(record["category_fd"], name, root_fd, recovery)
        _assert_fd_path(root_fd, root, "公司沙箱根")
        recovered = os.stat(recovery, dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISREG(recovered.st_mode)
                or (recovered.st_dev, recovered.st_ino) != (source.st_dev, source.st_ino)
                or _stat_content_signature(recovered) != record["expected"][:4]):
            raise OSError(f"整理恢复文件身份不一致：{recovery}")
        return recovery
    backup = record["backup"]
    if backup is None:
        raise OSError("整理路径在移动过程中失效，且没有可验证的安全副本")
    _assert_organize_backup_stat(root_fd, source, backup)
    _assert_fd_path(root_fd, root, "公司沙箱根")
    return backup


def _recover_organize_records(root_fd: int, root: Path,
                              records: list[dict]) -> None:
    errors: list[str] = []
    first_error: BaseException | None = None
    for record in records:
        if record.get("recovered"):
            continue
        try:
            _restore_organize_record(root_fd, root, record)
        except BaseException as exc:
            errors.append(f"{record['name']}: {exc}")
            if first_error is None:
                first_error = exc
    if errors:
        raise OSError("整理失败，以下文件恢复也失败：" + "；".join(errors)) from first_error


def _move_organize_files_posix(path: Path, moved: list[dict],
                              signatures: list[list[int]], company_id: str) -> None:
    root = company_storage_root(company_id)
    root_fd = _open_company_root_fd(company_id)
    target_fd = None
    category_fds: dict[str, int] = {}
    retired_category_fds: list[int] = []
    records: list[dict] = []
    try:
        target_fd = _open_relative_dir(root_fd, root, path)
        _assert_fd_path(root_fd, root, "公司沙箱根")
        _assert_fd_path(target_fd, path, "整理目录", root_fd=root_fd, root=root)
        for item, expected in zip(moved, signatures, strict=True):
            name = item["file"]
            category = item["category"]
            if (Path(name).name != name or name in ("", ".", "..")
                    or "/" in name or "\\" in name
                    or category not in FILE_CATEGORIES):
                raise OSError("整理计划包含非法文件或分类")
            expected_dest = path / category / name
            if Path(item["to"]) != expected_dest:
                raise OSError("整理计划目标路径不一致")
            source_stat = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
            if not stat.S_ISREG(source_stat.st_mode) or _stat_signature(source_stat) != expected:
                raise OSError(f"源文件在确认后发生变化：{name}")
            source_fd = os.open(name, _FILE_FLAGS, dir_fd=target_fd)
            backup = None
            try:
                if _stat_signature(os.fstat(source_fd)) != expected:
                    raise OSError(f"源文件在移动前发生变化：{name}")
                backup = _create_organize_backup(root_fd, target_fd, source_fd, name)
                category_fd = category_fds.get(category)
                if category_fd is not None:
                    try:
                        _assert_fd_path(category_fd, path / category, "分类目录",
                                        root_fd=root_fd, root=root)
                    except OSError:
                        retired_category_fds.append(category_fd)
                        del category_fds[category]
                        category_fd = None
                if category_fd is None:
                    try:
                        os.mkdir(category, dir_fd=target_fd)
                    except FileExistsError:
                        existing = os.stat(category, dir_fd=target_fd,
                                           follow_symlinks=False)
                        if not stat.S_ISDIR(existing.st_mode):
                            raise OSError(f"分类路径不是目录：{path / category}")
                    category_fd = os.open(category, _DIR_FLAGS, dir_fd=target_fd)
                    category_fds[category] = category_fd
                source_now = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
                if (not stat.S_ISREG(source_now.st_mode)
                        or _stat_content_signature(source_now) != expected[:4]
                        or source_now.st_ino != os.fstat(source_fd).st_ino):
                    raise OSError(f"源文件在移动时发生变化：{name}")
                _assert_fd_path(root_fd, root, "公司沙箱根")
                _assert_fd_path(target_fd, path, "整理目录",
                                root_fd=root_fd, root=root)
                _assert_fd_path(category_fd, path / category, "分类目录",
                                root_fd=root_fd, root=root)
                _rename_noreplace(target_fd, name, category_fd, name)
                try:
                    _assert_organize_destination(
                        root_fd, root, target_fd, path, category, category_fd,
                        source_fd, name, expected)
                    _assert_organize_backup(root_fd, source_fd, backup)
                except BaseException:
                    backup_error: BaseException | None = None
                    try:
                        _assert_organize_backup(root_fd, source_fd, backup)
                    except OSError:
                        backup = None
                        try:
                            backup = _create_organize_backup(
                                root_fd, category_fd, source_fd, name)
                        except BaseException as exc:
                            backup_error = exc
                    try:
                        _restore_organize_file(root_fd, category_fd, root,
                                               source_fd, backup, name, expected)
                    except BaseException as exc:
                        if backup_error is not None:
                            raise exc from backup_error
                        raise
                    if backup_error is not None:
                        raise backup_error
                    raise
                records.append({
                    "name": name,
                    "category": category,
                    "category_fd": category_fd,
                    "expected": expected,
                    "source_stat": os.fstat(source_fd),
                    "backup": backup,
                })
            finally:
                os.close(source_fd)

        for record in records:
            _validate_organize_record(root_fd, root, target_fd, path, record)
        for record in records:
            _cleanup_organize_record(root_fd, root, target_fd, path, record)
        for record in records:
            _validate_organize_record(root_fd, root, target_fd, path, record)
            _assert_fd_path(root_fd, root, "公司沙箱根")
            _assert_fd_path(target_fd, path, "整理目录",
                            root_fd=root_fd, root=root)
            _assert_fd_path(record["category_fd"], path / record["category"],
                            "分类目录", root_fd=root_fd, root=root)
    except BaseException:
        if records:
            _recover_organize_records(root_fd, root, records)
        raise
    finally:
        for fd in retired_category_fds:
            os.close(fd)
        for fd in category_fds.values():
            os.close(fd)
        if target_fd is not None:
            os.close(target_fd)
        os.close(root_fd)


_WINDOWS = os.name == "nt"


def _windows_rename_buffer(name: str, root_directory: int):
    import ctypes
    from ctypes import wintypes

    class RenameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_byte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded = name.encode("utf-16-le")
    rename = RenameInformation()
    rename.ReplaceIfExists = 0
    rename.RootDirectory = wintypes.HANDLE(root_directory)
    rename.FileNameLength = len(encoded)
    buffer = ctypes.create_string_buffer(
        ctypes.sizeof(rename) + rename.FileNameLength - ctypes.sizeof(wintypes.WCHAR))
    ctypes.memmove(buffer, ctypes.byref(rename), ctypes.sizeof(rename))
    ctypes.memmove(ctypes.addressof(buffer) + RenameInformation.FileName.offset,
                   encoded, rename.FileNameLength)
    return buffer, ctypes.sizeof(buffer)


def _move_organize_files_windows(path: Path, moved: list[dict],
                                 signatures: list[list[int]], company_id: str) -> None:
    import ctypes
    import ntpath
    from ctypes import wintypes

    root = company_storage_root(company_id)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    invalid_handle = ctypes.c_void_p(-1).value
    generic_read, delete_access = 0x80000000, 0x00010000
    share_read_write = 0x1 | 0x2
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    reparse_attribute = 0x400
    directory_attribute = 0x10
    file_rename_info = 11

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def check(result: bool, action: str) -> None:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def open_handle(target: Path, access: int) -> int:
        handle = kernel32.CreateFileW(
            _windows_nt_path(target), access, share_read_write, None,
            open_existing, backup_semantics | open_reparse_point, None,
        )
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def information(handle: int) -> FileInformation:
        value = FileInformation()
        check(kernel32.GetFileInformationByHandle(handle, ctypes.byref(value)), "读取文件身份失败")
        return value

    def identity(value: FileInformation) -> list[int]:
        return [
            value.dwVolumeSerialNumber,
            (value.nFileIndexHigh << 32) | value.nFileIndexLow,
            (value.nFileSizeHigh << 32) | value.nFileSizeLow,
            ((value.ftLastWriteTime.dwLowDateTime
              | (value.ftLastWriteTime.dwHighDateTime << 32))
             + 116444736000000000) * 100,
        ]

    def reject_reparse(target: Path) -> None:
        attributes = os.lstat(target).st_file_attributes
        if attributes & reparse_attribute:
            raise OSError(f"整理路径包含重解析点，拒绝访问：{target}")

    def open_directory(target: Path) -> int:
        reject_reparse(target)
        handle = open_handle(target, generic_read)
        try:
            attributes = information(handle).dwFileAttributes
            if attributes & reparse_attribute or not attributes & directory_attribute:
                raise OSError(f"整理路径不是普通目录：{target}")
            return handle
        except BaseException:
            kernel32.CloseHandle(handle)
            raise

    def final_path(handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        return ntpath.normcase(buffer.value)

    def require_child(parent: str, child: str, label: str) -> None:
        if child != parent and not child.startswith(parent + "\\"):
            raise OSError(f"{label} 已离开公司沙箱：{child}")

    for component in (root, *root.parents):
        if component == component.parent:
            break
        reject_reparse(component)
    relative = path.relative_to(root)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise OSError("整理目录路径不合法")
    root_handle = open_directory(root)
    target_handle = root_handle
    try:
        current = root
        for part in relative.parts:
            current = current / part
            next_handle = open_directory(current)
            if target_handle != root_handle:
                kernel32.CloseHandle(target_handle)
            target_handle = next_handle
        root_path = final_path(root_handle)
        target_path = final_path(target_handle)
        require_child(root_path, target_path, "整理目录")
        for item, expected in zip(moved, signatures, strict=True):
            name = item["file"]
            category = item["category"]
            if (Path(name).name != name or name in ("", ".", "..")
                    or "/" in name or "\\" in name
                    or category not in FILE_CATEGORIES):
                raise OSError("整理计划包含非法文件或分类")
            source = path / name
            expected_dest = path / category / name
            if Path(item["to"]) != expected_dest:
                raise OSError("整理计划目标路径不一致")
            reject_reparse(source)
            source_handle = open_handle(source, generic_read | delete_access)
            category_handle = None
            try:
                source_info = information(source_handle)
                source_identity = identity(source_info)
                if (source_info.dwFileAttributes & (reparse_attribute | directory_attribute)
                        or source_identity != expected[:4]):
                    raise OSError(f"源文件在确认后发生变化：{name}")
                try:
                    os.mkdir(path / category)
                except FileExistsError:
                    reject_reparse(path / category)
                category_handle = open_directory(path / category)
                category_path = final_path(category_handle)
                require_child(target_path, category_path, "分类目录")
                current_target = open_handle(path, generic_read)
                try:
                    if identity(information(current_target)) != identity(information(target_handle)):
                        raise OSError(f"整理目录在移动前被替换：{path}")
                finally:
                    kernel32.CloseHandle(current_target)
                source_info = information(source_handle)
                if (source_info.dwFileAttributes & (reparse_attribute | directory_attribute)
                        or identity(source_info) != expected[:4]):
                    raise OSError(f"源文件在移动时发生变化：{name}")
                buffer, size = _windows_rename_buffer(name, category_handle)
                check(kernel32.SetFileInformationByHandle(
                    source_handle, file_rename_info, buffer, size,
                ), f"移动失败，拒绝覆盖：{expected_dest}")
            finally:
                if category_handle is not None:
                    kernel32.CloseHandle(category_handle)
                kernel32.CloseHandle(source_handle)
    finally:
        if target_handle != root_handle:
            kernel32.CloseHandle(target_handle)
        kernel32.CloseHandle(root_handle)


def _windows_nt_path(path: Path) -> str:
    value = str(path.absolute())
    return value if value.startswith("\\\\?\\") else f"\\\\?\\{value}"


def _move_organize_files(path: Path, moved: list[dict],
                         signatures: list[list[int]], company_id: str) -> None:
    if _WINDOWS:
        _move_organize_files_windows(path, moved, signatures, company_id)
    else:
        _move_organize_files_posix(path, moved, signatures, company_id)


async def organize_folder(path: Path, dry_run: bool = False,
                          expected_preview_hash: str | None = None, *,
                          company_id: str) -> dict:
    """按类型整理指定公司的文件夹（图片/文档/表格/…子文件夹）。"""
    path = resolve_organize_target(path, company_id)
    if not path.exists():
        return {"ok": False, "error": f"路径不存在: {path}"}
    if not path.is_dir():
        return {"ok": False, "error": f"不是目录: {path}"}

    moved, skipped, signatures = [], [], []
    reserved: set[Path] = set()
    for f in sorted(path.iterdir(), key=lambda p: p.name):
        source_stat = f.lstat()
        if stat.S_ISDIR(source_stat.st_mode) or f.name.startswith("."):
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            skipped.append(f.name)
            continue
        ext = f.suffix.lower()
        category = next((cat for cat, exts in FILE_CATEGORIES.items()
                         if ext in exts), None)
        if not category:
            skipped.append(f.name)
            continue
        # 分类子目录同样过沙箱：防止有人预先在 storage 里放一个指向
        # 外部的符号链接目录，把文件「整理」到沙箱之外。
        dest_dir = resolve_organize_target(path / category, company_id)
        dest = dest_dir / f.name
        n = 1
        while dest.exists() or dest.is_symlink() or dest in reserved:
            stem, suffix = f.stem, f.suffix
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
        reserved.add(dest)
        moved.append({"file": f.name, "category": category, "to": str(dest)})
        signatures.append([
            source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
            source_stat.st_mtime_ns, source_stat.st_ctime_ns,
        ])

    preview_hash = _organize_preview_hash(path, moved, len(skipped), signatures)
    if expected_preview_hash and expected_preview_hash != preview_hash:
        return {"ok": False, "denied": True,
                "error": "目录内容或整理预览已变化，请重新预览后再确认",
                "message": "❌ 目录内容或整理预览已变化，未移动任何文件。请重新预览。"}
    if dry_run:
        return {"ok": True, "moved": moved, "skipped_count": len(skipped),
                "dry_run": True, "path": str(path), "preview_hash": preview_hash}

    await asyncio.to_thread(_move_organize_files, path, moved, signatures, company_id)
    return {"ok": True, "moved": moved, "skipped_count": len(skipped),
            "dry_run": False, "path": str(path), "preview_hash": preview_hash}


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


async def preview_organize_folder(raw_path: str | Path, company_id: str,
                                  agent_id: str | None = None) -> dict:
    """返回不可移动的整理预览及一次性确认令牌。"""
    from app.core.sandbox import SandboxViolation
    from app.services.computer_service import ComputerActionDenied

    if not str(raw_path or "").strip():
        return {"ok": False, "error": "缺少目标路径",
                "message": "❌ 缺少要整理的目录路径"}
    try:
        await _require_computer_permission(company_id, "run_write", agent_id)
    except ComputerActionDenied as exc:
        return {"ok": False, "denied": True, "error": exc.reason,
                "message": f"❌ 未授权：{exc.reason}"}
    try:
        target = resolve_organize_target(raw_path, company_id)
    except SandboxViolation as exc:
        logger.warning("NL organize_folder 预览越界：company=%s path=%r err=%s",
                       company_id, raw_path, exc)
        return {"ok": False, "denied": True, "error": str(exc),
                "message": f"❌ 拒绝整理：{exc}"}
    try:
        result = await organize_folder(target, dry_run=True, company_id=company_id)
    except (OSError, SandboxViolation) as exc:
        return {"ok": False, "denied": isinstance(exc, SandboxViolation),
                "error": str(exc), "message": f"❌ 预览失败：{exc}"}
    if not result.get("ok"):
        return result
    token = uuid.uuid4().hex
    return {"ok": True, "status": "preview", "awaiting_confirmation": True,
            "message": ("整理预览（尚未移动任何文件）：\n"
                        f"目标：{target}\n"
                        f"计划移动 {len(result['moved'])} 个文件，"
                        f"跳过 {result['skipped_count']} 个未识别类型。\n"
                        "请回复「确认整理」执行；更换目录请重新提出整理。"),
            "detail": result["moved"][:10],
            "path": result["path"], "preview_hash": result["preview_hash"],
            "preview_token": token}


async def execute_organize_preview(raw_path: str | Path, *, preview_token: str,
                                   expected_preview_hash: str,
                                   expected_path: str | None = None,
                                   company_id: str,
                                   agent_id: str | None = None) -> dict:
    """核对路径/预览后执行一次；权限和沙箱在此时重新校验。"""
    from app.core.sandbox import SandboxViolation
    from app.services.computer_service import ComputerActionDenied

    if not preview_token or not expected_preview_hash:
        return {"ok": False, "denied": True, "error": "缺少有效的整理预览确认",
                "message": "❌ 缺少有效的整理预览确认，未移动任何文件。"}
    try:
        await _require_computer_permission(company_id, "run_write", agent_id)
    except ComputerActionDenied as exc:
        return {"ok": False, "denied": True, "error": exc.reason,
                "message": f"❌ 未授权：{exc.reason}"}
    try:
        target = resolve_organize_target(raw_path, company_id)
    except SandboxViolation as exc:
        return {"ok": False, "denied": True, "error": str(exc),
                "message": f"❌ 拒绝执行：{exc}"}
    if expected_path is not None and str(target) != expected_path:
        return {"ok": False, "denied": True, "error": "整理路径与预览不一致",
                "message": "❌ 整理路径与预览不一致，未移动任何文件。请重新预览。"}
    try:
        result = await organize_folder(
            target, dry_run=False, expected_preview_hash=expected_preview_hash,
            company_id=company_id)
    except (OSError, SandboxViolation) as exc:
        return {"ok": False, "denied": isinstance(exc, SandboxViolation),
                "error": str(exc), "message": f"❌ 执行失败：{exc}"}
    if not result.get("ok"):
        return result
    moved_by_cat: dict[str, int] = {}
    for item in result.get("moved", []):
        moved_by_cat[item["category"]] = moved_by_cat.get(item["category"], 0) + 1
    summary = "、".join(f"{k}×{v}" for k, v in moved_by_cat.items())
    msg = f"✅ 整理完成！共移动 {len(result.get('moved', []))} 个文件"
    if summary:
        msg += f"（{summary}）"
    if result.get("skipped_count"):
        msg += f"，跳过 {result['skipped_count']} 个未识别类型"
    return {"ok": True, "status": "completed", "message": msg,
            "detail": result.get("moved", [])[:10], "path": str(target)}


async def execute_nl(intent: dict, company_id: str,
                     agent_id: str | None = None) -> dict:
    """执行识别出的意图。文件整理永远只返回预览，不直接移动。"""
    from app.services.computer_service import ComputerActionDenied

    action = (intent or {}).get("action")
    capability = ACTION_CAPABILITY.get(action)
    if not capability:
        return {"ok": False, "error": f"未知操作: {action}"}

    if action == "organize_folder":
        raw_path = (intent.get("params") or {}).get("path")
        return await preview_organize_folder(raw_path, company_id, agent_id)

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
        return {"ok": ok, "status": "completed" if ok else "failed",
                "message": f"✅ 已打开 {app}" if ok
                else f"❌ 打不开 {app}: {err.decode()[:100]}"}
    return {"ok": False, "error": f"未知操作: {action}"}
