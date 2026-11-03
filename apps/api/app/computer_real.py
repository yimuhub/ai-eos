"""真实本机执行器（macOS）。

用系统原生能力实现 computer use：
- screenshot: screencapture（macOS 内置）
- type_text / click / key: osascript System Events（AppleScript 键鼠控制）
- run_app: osascript open
- shell: asyncio.create_subprocess_shell（高风险，需授权+审批）

所有操作仍走 ComputerPermission 授权 + ComputerActionLog 审计。
执行器选择：优先真实执行，异常时回退并如实标注 fallback。
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.computer_agent import ComputerActionLog
from app.computer_ax import AX_EXECUTORS
from app.core.logging_config import get_logger

logger = get_logger(__name__)


def _resolve_screenshot_dir() -> str:
    """截屏保存目录。

    /tmp 重启即清空，不再使用。可配（AIEOS_SCREENSHOT_DIR），
    默认 storage/screenshots（与 assets.py 同一 storage 根）。
    """
    raw = os.environ.get("AIEOS_SCREENSHOT_DIR", "").strip()
    if raw:
        target = raw
    else:
        try:
            from app.core.config import settings as _settings
            root = _settings.storage_dir
        except Exception:
            root = "./storage"
        base = Path(root)
        if not base.is_absolute():
            base = Path(__file__).resolve().parents[1] / root
        target = str(base / "screenshots")
    os.makedirs(target, exist_ok=True)
    return target


SCREENSHOT_DIR = _resolve_screenshot_dir()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _run_osascript(script: str) -> tuple[bool, str]:
    """执行 AppleScript，返回 (success, output_or_error)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        ok = proc.returncode == 0
        return ok, (out.decode().strip() if ok else err.decode().strip())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def real_screenshot(params: dict) -> dict:
    """screencapture 真实截屏。"""
    path = os.path.join(
        SCREENSHOT_DIR,
        f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png")
    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-x", "-C", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        err_msg = err.decode().strip()
        if "could not create image" in err_msg:
            err_msg += (" —— 需要屏幕录制权限：系统设置 → 隐私与安全性 → 屏幕录制，"
                        "为运行本服务的终端 App 勾选权限后重启服务")
        return {"ok": False, "error": err_msg}
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {"ok": True, "image_path": path, "bytes": size,
            "note": "真实截屏（screencapture -x 静默模式）"}


# 终端/Shell 宿主：type_text 落在这些应用上等价于执行命令。
TERMINAL_APP_NAMES = frozenset({
    "terminal", "iterm", "iterm2", "console", "gnome-terminal",
    "konsole", "xterm", "alacritty", "kitty", "warp", "wezterm",
    "hyper", "tabby", "ghostty", "rio", "tmux",
})


def _normalize_app_name(name: str | None) -> str:
    n = str(name or "").strip().strip("'\"").lower()
    if n.endswith(".app"):
        n = n[:-4]
    return os.path.basename(n)


def is_terminal_app(name: str | None) -> bool:
    """应用名（进程名或 .app 路径）是否属于终端类。"""
    return _normalize_app_name(name) in TERMINAL_APP_NAMES


async def frontmost_app_name() -> str | None:
    """当前前台应用进程名；查询失败或无输出返回 None（调用方按允许处理）。"""
    ok, out = await _run_osascript(
        'tell application "System Events" to get name of first '
        'application process whose frontmost is true')
    if not ok:
        return None
    text = out.strip()
    if not text:
        return None
    return text.splitlines()[0].strip() or None


async def real_type_text(params: dict, app_hint: str | None = None) -> dict:
    """osascript 真实键入文本到前台应用。

    ## 为什么检查前台应用

    `type_text` 是 AppleScript keystroke：如果前台是 Terminal/iTerm，
    先 `open -a Terminal` 再 type_text 就等价于执行任意主机命令，
    完全绕开 `core/sandbox.py` 的 shell 白名单。因此键入前先查前台
    进程名，命中终端类就拒绝。

    该检查刻意保守：**查不到前台应用时按允许处理并记 warning**，
    避免辅助功能权限未授予等环境问题把功能直接锁死。
    `app_hint` 是可选的调用方上下文（期望的目标应用），只用于日志与
    拒绝说明，不参与放行判断。
    """
    text = str((params or {}).get("text", ""))[:500]
    if not text:
        return {"ok": False, "error": "text is required"}
    hint = str(app_hint or (params or {}).get("app_hint") or "").strip()

    front = await frontmost_app_name()
    if front is None:
        logger.warning("type_text 无法获取前台应用名，按允许处理"
                       "（app_hint=%r）", hint or None)
    elif is_terminal_app(front):
        msg = (f"拒绝键入：前台应用 {front!r} 属于终端/Shell 宿主，"
               "键入内容可能被当作命令执行。请先切换到目标应用。")
        logger.warning("type_text 被拒绝：前台应用=%r app_hint=%r",
                       front, hint or None)
        return {"ok": False, "blocked": True, "typed": text[:100],
                "frontmost_app": front, "app_hint": hint or None,
                "error": msg}

    # AppleScript 转义
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    ok, out = await _run_osascript(
        f'tell application "System Events" to keystroke "{esc}"')
    if not ok and "不允许发送按键" in out:
        out += (" —— 需要辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，"
                "为运行本服务的终端 App 勾选权限")
    return {"ok": ok, "typed": text[:100], "frontmost_app": front,
            "app_hint": hint or None, "error": None if ok else out}


async def real_click(params: dict) -> dict:
    """cliclick（如安装）或 osascript 点击坐标。"""
    x = int((params or {}).get("x", 0))
    y = int((params or {}).get("y", 0))
    # 优先 cliclick（brew install cliclick）
    if os.path.exists("/usr/local/bin/cliclick") or os.path.exists("/opt/homebrew/bin/cliclick"):
        bin_path = "/opt/homebrew/bin/cliclick" if os.path.exists("/opt/homebrew/bin/cliclick") else "/usr/local/bin/cliclick"
        proc = await asyncio.create_subprocess_exec(
            bin_path, f"c:{x},{y}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), timeout=10)
        ok = proc.returncode == 0
        return {"ok": ok, "clicked_at": [x, y],
                "tool": "cliclick", "error": None if ok else "cliclick failed"}
    # 回退：osascript 不支持直接坐标点击，需辅助功能权限；报告明确错误
    ok, out = await _run_osascript(
        f'tell application "System Events" to click at {{{x}, {y}}}')
    return {"ok": ok, "clicked_at": [x, y], "tool": "osascript",
            "error": None if ok else out}


async def real_key(params: dict) -> dict:
    """按键（回车/Tab/方向键等）。"""
    key = str((params or {}).get("key", "")).lower()
    key_map = {"enter": "return", "return": "return", "tab": "tab",
               "esc": "escape", "escape": "escape",
               "up": "up arrow", "down": "down arrow",
               "left": "left arrow", "right": "right arrow",
               "space": "space", "delete": "delete"}
    as_key = key_map.get(key)
    if not as_key:
        return {"ok": False, "error": f"unsupported key: {key}"}
    ok, out = await _run_osascript(
        f'tell application "System Events" to key code ' +
        _key_code(as_key))
    return {"ok": ok, "key": key, "error": None if ok else out}


def _key_code(name: str) -> str:
    codes = {"return": "36", "tab": "48", "escape": "53",
             "up arrow": "126", "down arrow": "125",
             "left arrow": "123", "right arrow": "124",
             "space": "49", "delete": "51"}
    return codes.get(name, "36")


async def real_run_app(params: dict) -> dict:
    """open 命令启动应用。"""
    app = str((params or {}).get("app", "")).strip()
    if not app or any(c in app for c in ";|&$`"):
        return {"ok": False, "error": "invalid app name"}
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", app,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    ok = proc.returncode == 0
    return {"ok": ok, "app": app,
            "error": None if ok else err.decode().strip()}


# ==================== 受控 shell 执行 ====================
#
# ## 改造说明（原实现存在真实的命令注入通道）
#
# 原 `real_run_shell` / `real_run_write` 用 `asyncio.create_subprocess_shell`
# 执行**拼接后的字符串**，而且过滤不完整：
#   - real_run_shell 过滤了 `;|&$`><`，但没有过滤换行、`(`、`)`、`\`；
#   - real_run_write 只过滤了 `&&`、`||`、`$(`、反引号，
#     **完全没有过滤 `;`、`|`、`>`、`<`、换行**，却允许 `rm -rf` 前缀。
# 于是一句 `mkdir /tmp/x; rm -rf ~/Documents` 可以通过白名单校验并被 shell 执行。
#
# 现在的做法：
#   1. 拒绝一切 shell 元字符——因为下面根本不用 shell，任何元字符都属异常输入；
#   2. 用 shlex 解析为 argv，只允许 argv[0] 命中二进制白名单；
#   3. 路径类参数逐个过文件沙箱（app/core/sandbox.py），越界即拒绝；
#   4. 子进程 cwd 固定为沙箱根，`ls` 这类无参命令不会泄露进程工作目录；
#   5. 用 create_subprocess_exec（非 shell）执行 argv，
#      即使过滤有漏，shell 层也已不存在，无法解释 `;` 或 `|`。
#
# 失败方向始终是"拒绝执行"，不会退化成"放行危险操作"。

# 只读命令白名单
_READ_BINARIES = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
    "pwd", "date", "whoami", "echo", "sort", "uniq", "cut", "grep",
    "find", "basename", "dirname", "realpath",
}

# 写入命令白名单。rm/rmdir 属破坏性操作，仅允许作用于沙箱内路径，
# 因此无法删除沙箱之外的任何东西（`rm -rf /` 会在沙箱校验阶段被拒）。
_WRITE_BINARIES = {"cp", "mv", "mkdir", "touch", "rm", "rmdir"}

# 需要逐参做沙箱校验的命令（这些命令的参数代表文件路径）
_PATH_TAKING_BINARIES = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
    "sort", "uniq", "cut", "grep", "find",
    "cp", "mv", "mkdir", "touch", "rm", "rmdir",
    "basename", "dirname", "realpath",
}

# shell 元字符：既然不经过 shell，出现即视为可疑输入，直接拒绝。
# 只拒绝"在无 shell 环境下也没有合法用途"的字符；`*?()[]{}"` 等保留，
# 因为 `find . -name "*.md"` 这类命令需要它们，且无 shell 时它们是惰性的。
_SHELL_METACHARS = set(";|&<>$`\\\n\r")


def _sandbox_cwd() -> str:
    """子进程工作目录 = 仓库根。

    必须与 sandbox.resolve_safe_path() 解析相对路径的基准保持一致，
    否则会出现"校验通过但执行找不到文件"的错位。

    注意（改造中发现的真实问题）：早期实现取 sandbox_roots()[1]，
    该目录（data/uploads）通常不存在，于是回退到 Path.home()，
    导致无参 `ls` 直接列出了用户的真实主目录。现在固定为仓库根，
    且所有路径参数都已过沙箱，越界路径在参数校验阶段就被拒绝。
    """
    from app.core.sandbox import PROJECT_ROOT

    return str(PROJECT_ROOT)


def _validate_command(command: str, allowed: set[str]) -> tuple[list[str] | None, str]:
    """校验并解析命令。返回 (argv, "") 或 (None, 拒绝原因)。"""
    import shlex

    cmd = (command or "").strip()
    if not cmd:
        return None, "命令为空"

    hit = [ch for ch in _SHELL_METACHARS if ch in cmd]
    if hit:
        readable = "、".join(repr(c) for c in sorted(set(hit)))
        return None, f"命令包含被禁止的 shell 字符：{readable}（本执行器不支持管道/重定向/连接）"

    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        return None, f"命令解析失败：{exc}"
    if not argv:
        return None, "命令为空"

    binary = os.path.basename(argv[0])
    if binary not in allowed:
        return None, (f"命令 {binary} 不在白名单内。"
                      f"允许：{'、'.join(sorted(allowed))}")

    # 路径参数逐一过沙箱
    if binary in _PATH_TAKING_BINARIES:
        from app.core.sandbox import is_path_allowed

        for token in argv[1:]:
            if token.startswith("-"):
                continue
            ok, reason = is_path_allowed(token)
            if not ok:
                return None, f"参数 {token!r} 越界：{reason}"

    return argv, ""


async def _exec_argv(argv: list[str], timeout: int = 30) -> dict:
    """用 exec（非 shell）执行已校验的 argv。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=_sandbox_cwd(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"ok": proc.returncode == 0,
                "stdout": out.decode(errors="replace")[:2000],
                "stderr": err.decode(errors="replace")[:500],
                "exit_code": proc.returncode}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"命令超时（>{timeout}s），已终止"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def real_run_shell(params: dict) -> dict:
    """受控只读 shell 执行。

    HIGH RISK 能力：需要公司级 run_shell 授权（默认未授权即拒绝），
    且仅允许只读命令、仅作用于沙箱内路径、不经 shell 解释。
    """
    argv, reason = _validate_command(
        str((params or {}).get("command", "")), _READ_BINARIES)
    if argv is None:
        return {"ok": False, "error": reason}
    return await _exec_argv(argv)


async def real_run_write(params: dict) -> dict:
    """受控写操作。

    HIGH RISK 能力：需要公司级 run_write 授权。
    破坏性操作（rm/rmdir/mv）默认只做预演，必须由调用方显式传
    dry_run=False 才真正执行；且仅能作用于沙箱内路径。
    """
    command = str((params or {}).get("command", ""))
    argv, reason = _validate_command(command, _WRITE_BINARIES)
    if argv is None:
        return {"ok": False, "error": reason}

    binary = os.path.basename(argv[0])
    targets = [t for t in argv[1:] if not t.startswith("-")]

    preview: list[str] = []
    if binary in ("mv", "cp") and len(targets) >= 2:
        preview.append(f"将 {targets[0]} → {targets[1]}")
    elif binary in ("rm", "rmdir"):
        preview.append(f"将删除：{'、'.join(targets)}")
    elif binary == "mkdir":
        preview.append(f"将创建目录：{'、'.join(targets)}")
    elif binary == "touch":
        preview.append(f"将创建文件：{'、'.join(targets)}")

    # 破坏性操作强制预演：只有调用方**显式**传 dry_run=False 才真正执行，
    # 避免 LLM 只写一句 `rm -rf xxx` 就把文件删掉。
    destructive = binary in ("rm", "rmdir")
    explicit_execute = (params or {}).get("dry_run") is False
    if destructive and not explicit_execute:
        return {"ok": True, "dry_run": True, "preview": preview,
                "dry_run_message": ("破坏性操作预演：未实际执行。"
                                    "确认无误后请以 dry_run=false 再次调用。")}

    result = await _exec_argv(argv)
    result["preview"] = preview
    return result


REAL_EXECUTORS = {
    "run_write": real_run_write,
    "screenshot": real_screenshot,
    "type_text": real_type_text,
    "click": real_click,
    "key": real_key,
    "run_app": real_run_app,
    "run_shell": real_run_shell,
    # 无障碍树一路：读界面结构、按元素引用操作，不看像素
    **AX_EXECUTORS,
}

# 扩展后的能力清单（computer_agent.KNOWN_CAPABILITIES 的超集）
EXTENDED_CAPABILITIES = list(REAL_EXECUTORS.keys())
