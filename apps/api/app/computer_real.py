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

from app.computer_agent import ComputerActionLog

SCREENSHOT_DIR = "/tmp/ai-eos-screens"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


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


async def real_type_text(params: dict) -> dict:
    """osascript 真实键入文本到前台应用。"""
    text = str((params or {}).get("text", ""))[:500]
    if not text:
        return {"ok": False, "error": "text is required"}
    # AppleScript 转义
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    ok, out = await _run_osascript(
        f'tell application "System Events" to keystroke "{esc}"')
    if not ok and "不允许发送按键" in out:
        out += (" —— 需要辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，"
                "为运行本服务的终端 App 勾选权限")
    return {"ok": ok, "typed": text[:100], "error": None if ok else out}


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


async def real_run_shell(params: dict) -> dict:
    """受控 shell 执行（HIGH RISK——需要 run_shell 授权 + 白名单命令）。"""
    ALLOWED_PREFIXES = ("ls", "cat ", "head ", "tail ", "pwd", "echo ",
                        "date", "whoami", "df ", "du ", "wc ", "find ")
    cmd = str((params or {}).get("command", "")).strip()
    if not cmd or any(c in cmd for c in ";|&$`><"):
        return {"ok": False, "error": "command contains forbidden characters"}
    if not cmd.startswith(ALLOWED_PREFIXES):
        return {"ok": False,
                "error": f"command not in whitelist (allowed prefixes: {ALLOWED_PREFIXES})"}
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {"ok": proc.returncode == 0,
                "stdout": out.decode()[:2000],
                "stderr": err.decode()[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def real_run_write(params: dict) -> dict:
    """Run write shell command (mv/cp/mkdir/rm/touch) with safety checks."""
    READ_PREFIXES = ("ls", "cat ", "head ", "tail ", "pwd", "echo ",
                     "date", "whoami", "df ", "du ", "wc ", "find ")
    WRITE_PREFIXES = ("mv ", "cp ", "mkdir ", "touch ", "rm -rf ", "rm -r ",
                      "rm ", "cat ", "head ", "tail ")
    cmd = str((params or {}).get("command", "")).strip()
    if not cmd:
        return {"ok": False, "error": "empty command"}
    DANGEROUS = ("&&", "||", "$(", "`")
    if any(d in cmd for d in DANGEROUS):
        return {"ok": False, "error": "forbidden operators: " + str(DANGEROUS)}
    ok = cmd.startswith(READ_PREFIXES) or cmd.startswith(WRITE_PREFIXES)
    if not ok:
        return {"ok": False, "error": "command not in whitelist"}
    is_dry = "--dry-run" in cmd or " -n " in cmd
    preview = []
    if cmd.startswith(("mv ", "cp ")):
        parts = cmd.split()
        if len(parts) >= 3:
            preview.append(f"将: {parts[1]} → {parts[2]}")
    elif cmd.startswith("rm"):
        preview.append(f"将删除: {cmd[len('rm'):].strip()}")
    elif cmd.startswith("mkdir"):
        preview.append(f"将创建目录: {cmd[len('mkdir'):].strip()}")
    elif cmd.startswith("touch"):
        preview.append(f"将创建文件: {cmd[len('touch'):].strip()}")
    try:
        if is_dry:
            return {"ok": True, "dry_run": True, "preview": preview,
                    "dry_run_message": "dry-run 模式，未执行实际写入。"}
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {"ok": proc.returncode == 0, "preview": preview,
                "stdout": out.decode()[:2000], "stderr": err.decode()[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


REAL_EXECUTORS = {
    "run_write": real_run_write,
    "screenshot": real_screenshot,
    "type_text": real_type_text,
    "click": real_click,
    "key": real_key,
    "run_app": real_run_app,
    "run_shell": real_run_shell,
}

# 扩展后的能力清单（computer_agent.KNOWN_CAPABILITIES 的超集）
EXTENDED_CAPABILITIES = list(REAL_EXECUTORS.keys())
