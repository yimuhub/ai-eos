"""本机操作策略：三档审批 + 自动放行 + 硬拦截。

三档模式的边界：
* `default`：只读动作直接执行，写文件 / 命令 / 键鼠每次审批；
* `auto`：白名单内的目录、命令前缀、应用放行，白名单外仍审批；
* `full`：限时完全控制，到期自动降回 `default`，不静默续期。

硬拦截不受模式影响，也绝不落库：凭据目录、灾难性命令、向终端宿主键入，
在代码里写死，前端只能读取展示，不能改写。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plugin import ComputerPolicy

MODES = ("default", "auto", "full")

OBSERVE_ACTIONS = {
    "screenshot", "list_windows", "read_file", "accessibility_snapshot",
    "get_window", "find_element",
}
COMMAND_ACTIONS = {"run_shell", "run_command", "execute_command", "shell"}
WRITE_ACTIONS = {"run_write", "write_file", "file_write", "write"}
APP_ACTIONS = {"run_app", "open_app", "app_launch"}
# 界面上的写操作：按引用点击 / 赋值。风险与键鼠注入同级，但目标应用藏在
# 元素引用里（不在 app 参数上），所以白名单匹配要走 _ref_app。
UI_WRITE_ACTIONS = {"click_element", "set_value"}

HARD_BLOCK_DIRS = [
    "~/.ssh", "~/.aws", "~/.gnupg", "~/Library/Keychains",
    "/etc", "/System", "/bin", "/sbin", "/usr/bin", "/usr/sbin",
    "/dev", "/boot", "/var/db", "C:\\Windows",
]
HARD_BLOCK_NAMES = {
    ".env", ".env.local", "id_rsa", "id_ed25519", "id_ecdsa",
    "credentials", "known_hosts", "authorized_keys",
}
HARD_BLOCK_COMMAND_PATTERNS = [
    (r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/\s*$", "灾难性删除：rm -rf /"),
    (r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r?\s+/\s*$", "灾难性删除：rm -rf /"),
    (r"\bmkfs(\.|\s)", "磁盘格式化"),
    (r"\bdiskutil\s+erase", "磁盘擦除"),
    (r"\bdd\s+if=", "dd 直写设备"),
    (r"\b(shutdown|halt|reboot)\b", "关机 / 重启"),
    (r"chmod\s+-R\s+777\s+/", "系统目录提权"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", "fork 炸弹"),
]
TERMINAL_HOSTS = {
    "terminal", "iterm", "iterm2", "warp", "kitty", "alacritty",
    "hyper", "tabby", "cmux", "powershell", "cmd", "windowsterminal",
}
_SHELL_METACHARS = re.compile(r"[;&|`$<>]")
_PATH_KEYS = ("path", "file", "filepath", "file_path", "target", "dir",
              "directory", "cwd", "destination", "output")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_policy() -> dict:
    return {
        "mode": "default",
        "auto_dirs": [],
        "auto_commands": [],
        "auto_apps": [],
        "full_control_until": None,
        "notify_on_finish": True,
        "updated_by": None,
    }


def serialize_policy(row: ComputerPolicy | None) -> dict:
    if not row:
        return default_policy() | {"id": None, "configured": False}
    return {
        "id": row.id,
        "mode": row.mode or "default",
        "auto_dirs": row.auto_dirs or [],
        "auto_commands": row.auto_commands or [],
        "auto_apps": row.auto_apps or [],
        "full_control_until": (row.full_control_until.isoformat()
                               if row.full_control_until else None),
        "notify_on_finish": row.notify_on_finish,
        "updated_by": row.updated_by,
        "configured": True,
    }


async def get_policy_row(db: AsyncSession, company_id: str) -> ComputerPolicy | None:
    """读策略不落库：没配置过就返回 None，由调用方走旧行为。"""
    return (await db.execute(select(ComputerPolicy).where(
        ComputerPolicy.company_id == company_id))).scalars().first()


async def upsert_policy(db: AsyncSession, company_id: str, user_id: str,
                        *, mode: str | None = None,
                        auto_dirs: list[str] | None = None,
                        auto_commands: list[str] | None = None,
                        auto_apps: list[str] | None = None,
                        notify_on_finish: bool | None = None) -> ComputerPolicy:
    row = await get_policy_row(db, company_id)
    if not row:
        row = ComputerPolicy(company_id=company_id, mode="default",
                             auto_dirs=[], auto_commands=[], auto_apps=[],
                             notify_on_finish=True)
        db.add(row)
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"未知审批模式 {mode!r}")
        row.mode = mode
        if mode != "full":
            row.full_control_until = None
    if auto_dirs is not None:
        row.auto_dirs = [d.strip() for d in auto_dirs if d.strip()][:50]
    if auto_commands is not None:
        row.auto_commands = [c.strip() for c in auto_commands if c.strip()][:50]
    if auto_apps is not None:
        row.auto_apps = [a.strip() for a in auto_apps if a.strip()][:50]
    if notify_on_finish is not None:
        row.notify_on_finish = notify_on_finish
    row.updated_by = user_id
    await db.commit()
    await db.refresh(row)
    return row


def start_full_control(row: ComputerPolicy, minutes: int) -> ComputerPolicy:
    minutes = max(5, min(int(minutes), 240))
    row.mode = "full"
    row.full_control_until = _now() + timedelta(minutes=minutes)
    return row


def exit_full_control(row: ComputerPolicy) -> ComputerPolicy:
    row.mode = "default"
    row.full_control_until = None
    return row


def full_control_active(row: ComputerPolicy | None) -> bool:
    return bool(row and row.mode == "full" and row.full_control_until
                and row.full_control_until > _now())


def hard_blocks() -> list[dict]:
    return [
        {"kind": "dirs", "label": "凭据与系统目录",
         "examples": HARD_BLOCK_DIRS},
        {"kind": "files", "label": "密钥与环境变量文件",
         "examples": sorted(HARD_BLOCK_NAMES)},
        {"kind": "commands", "label": "灾难性命令",
         "examples": list(dict.fromkeys(
             label for _, label in HARD_BLOCK_COMMAND_PATTERNS))},
        {"kind": "terminal", "label": "向终端宿主键入 / 界面赋值",
         "examples": sorted(TERMINAL_HOSTS)},
    ]


def _expanded(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([_expanded(path), _expanded(directory)]) == _expanded(directory)
    except (ValueError, OSError):
        return False


def _param_paths(params: dict) -> list[str]:
    return [str(params[key]) for key in _PATH_KEYS
            if params.get(key) not in (None, "")]


def hard_block_reason(action_type: str, params: dict | None) -> str | None:
    """硬红线单独可查：审批通过后执行、聊天工具调用也必须在最后一个检查点上问一次。

    只靠入口检查是不够的——`intervention` 批准后会直接调执行服务，
    一条在创建审批单时合规、在执行时已经越线的操作（比如审批期间被
    改过的 params）不该因为"曾经放过"就绕过红线。
    """
    return _hard_block(action_type, params or {})


def _hard_block(action_type: str, params: dict) -> str | None:
    for path in _param_paths(params):
        base = os.path.basename(path.rstrip("/")).lower()
        if base in HARD_BLOCK_NAMES or base.startswith(".env"):
            return f"硬拦截：不允许操作密钥 / 环境变量文件 {base}"
        for blocked in HARD_BLOCK_DIRS:
            if _within(path, blocked):
                return f"硬拦截：不允许操作受保护目录 {blocked}"
    command = str(params.get("command") or params.get("cmd") or "")
    if command:
        lowered = command.strip().lower()
        for pattern, label in HARD_BLOCK_COMMAND_PATTERNS:
            if re.search(pattern, lowered):
                return f"硬拦截：{label}"
    if action_type == "type_text":
        app = str(params.get("app") or params.get("application") or "").lower()
        if any(host in app for host in TERMINAL_HOSTS):
            return "硬拦截：不允许向终端宿主应用键入内容"
    if action_type == "set_value" and any(
            host in _ref_app(params) for host in TERMINAL_HOSTS):
        return ("硬拦截：不允许把内容写进终端宿主应用的界面——"
                "那等价于提交一条命令，请改走受审批的 run_shell")
    return None


def _ref_app(params: dict) -> str:
    """从元素引用 `应用|窗口|路径` 里取应用名（小写、已解码）。

    引用是 accessibility_snapshot / find_element 生成的，进程名可能含空格或
    中文，所以只按第一个 `|` 切，不解析整条路径。
    """
    from urllib.parse import unquote

    ref = str(params.get("ref") or "")
    return unquote(ref.split("|", 1)[0]).strip().lower()


def _match_auto(row: ComputerPolicy, action_type: str,
                params: dict) -> str | None:
    if action_type in COMMAND_ACTIONS:
        command = str(params.get("command") or params.get("cmd") or "").strip()
        if not command or _SHELL_METACHARS.search(command):
            return None
        for prefix in row.auto_commands or []:
            if command == prefix or command.startswith(prefix + " "):
                return f"命令白名单：{prefix}"
        return None
    if action_type in WRITE_ACTIONS:
        for path in _param_paths(params):
            for directory in row.auto_dirs or []:
                if _within(path, directory):
                    return f"目录白名单：{directory}"
        return None
    if action_type in APP_ACTIONS or action_type in UI_WRITE_ACTIONS:
        app = str(params.get("app") or params.get("application")
                  or params.get("name") or "").strip().lower()
        if not app and action_type in UI_WRITE_ACTIONS:
            app = _ref_app(params)
        for allowed in row.auto_apps or []:
            if app and app == allowed.strip().lower():
                return f"应用白名单：{allowed}"
    return None


def evaluate_action(row: ComputerPolicy | None, action_type: str,
                    params: dict | None = None) -> dict:
    """返回 decision：allow / approval / blocked。

    `row=None` 表示公司还没配置过策略：只拦截硬规则，其余交给旧审批逻辑，
    避免升级后把已有自动化流程突然卡住。
    """
    params = params or {}
    block = _hard_block(action_type, params)
    if block:
        return {"decision": "blocked", "mode": "hard", "reason": block,
                "matched": "hard_block"}

    if row is None:
        return {"decision": "legacy", "mode": "unconfigured",
                "reason": "尚未配置电脑权限，沿用现有审批规则",
                "matched": None}

    mode = row.mode or "default"
    if mode == "full" and full_control_active(row):
        return {"decision": "allow", "mode": "full",
                "reason": "完全控制会话进行中", "matched": "full_session"}
    if mode == "full":
        mode = "default"

    if action_type in OBSERVE_ACTIONS:
        return {"decision": "allow", "mode": mode,
                "reason": "只读操作直接执行", "matched": "observe"}

    if mode == "auto":
        hit = _match_auto(row, action_type, params)
        if hit:
            return {"decision": "allow", "mode": "auto",
                    "reason": hit, "matched": hit}
        return {"decision": "approval", "mode": "auto",
                "reason": "不在自动放行白名单内，需要你确认", "matched": None}

    return {"decision": "approval", "mode": "default",
            "reason": "默认审批：写操作 / 命令 / 键鼠需要你确认", "matched": None}
