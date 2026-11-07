#!/usr/bin/env python3
"""AIEOS 设备端 sidecar：让 AI 员工在**这台机器**上动手。

后端只判断「这个动作该不该做」（三档策略 + 审批门），真正执行搬到这里。
之所以不能只靠后端判断：后端的规则是照着它自己那台机器写的——同一条路径
在这台机器上可能根本不存在，也可能正是老板不许碰的目录。所以这里是**第二把锁**：
领取到的命令先过本机护栏（可写根目录、只读命令白名单、凭据目录），
过不了就在本地拒绝并回报，绝不"服务器说可以我就跑"。

只用标准库：目标机器不需要装 Python 依赖、不需要 clone 仓库。

    # 1) 老板在「设备操作台」建设备，拿到一次性接入码
    # 2) 在这台机器上接入并启动（可写目录必须显式给，不给就是只读设备）
    python3 aieos_sidecar.py enroll --server http://192.168.1.20:8000 \
        --code <接入码> --allow-root ~/work/project
    python3 aieos_sidecar.py run

配置与设备密钥存在 ~/.aieos/sidecar.json（0600）。服务端吊销后本机这份配置
留着也无害——领不到命令。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AGENT_VERSION = "1.0.0"
CONFIG_PATH = Path(os.environ.get("AIEOS_SIDECAR_CONFIG")
                   or Path.home() / ".aieos" / "sidecar.json")
BACKUP_DIR = Path.home() / ".aieos" / "backups"

# 这台机器能真的执行哪些能力。截图/键鼠/无障碍树依赖各系统的原生工具，
# sidecar v1 只做「改代码 + 跑命令」这一组——报上去的能力必须做得到，
# 否则后端会把命令派给一个只会失败的机器。
CORE_CAPABILITIES = ("run_shell", "read_file", "write_file", "apply_patch")

# 领一条命令时服务端最多替这台设备等这么久。在线窗口是 45 秒，
# 所以这个值必须显著小于 45，否则命令会在等待中被服务端判离线。
CLAIM_WAIT_SECONDS = 12.0
SHELL_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20_000
MAX_DIFF_CHARS = 20_000

# ---------------------------------------------------------------- 本机护栏

# 凭据与系统目录：读了可能把密钥外发，写了就是砖。任何参数都不放行。
DENY_PATH_FRAGMENTS = (
    "/.ssh", "/.aws", "/.gnupg", "/.config/gcloud", "/.azure", "/.kube",
    "/library/keychains", "/etc/", "/private/etc/", "/system/",
    "/usr/", "/bin/", "/sbin/", "/var/run/",
    "/windows/", "/program files/", "/programdata/", "/system volume information/",
)

# 文件名红条：`.env` 里是密钥，`.git` 里的 hook 能变成任意命令执行。
DENY_NAME_FRAGMENTS = (
    "/.git", "/.env", ".pem", ".key", "id_rsa", "id_ed25519", "known_hosts",
    "credentials", "/.netrc", ".p12", ".pfx", ".kdbx",
)

# 与后端策略同一口径的灾难性片段。这里再判一次是因为设备可能跑在
# 完全不懂 ai-eos 的机器上，本地也不该被一次远程审批带走。
DESTRUCTIVE_FRAGMENTS = (
    "rm -rf /", "rm -rf ~", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
    "drop table", "drop database", "truncate table", "> /dev/sd",
)

# 命令 substitution 让"这条命令到底做了什么"无法静态判断，直接拒。
SUBSTITUTION_MARKS = ("$(", "`", "${")

# 默认可执行的命令：**只读**排查工具。改文件走 write_file / apply_patch
# （有 diff、有备份、可回滚），不走 shell。
READ_ONLY_COMMANDS = frozenset("""
ls cat head tail less more grep egrep fgrep rg ag find fd wc sort uniq cut tr
paste diff cmp stat file du df pwd whoami id uname hostname date env printenv
which whereis type man realpath basename dirname readlink ps pgrep lsof ping
git docker brew pip pip3 npm echo printf true test
""".split())

# 这些命令本身能写东西/改历史，只放行只读子命令。
READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"status", "diff", "log", "show", "branch", "blame",
                      "remote", "ls-files", "describe", "rev-parse"}),
    "docker": frozenset({"ps", "images", "inspect", "logs", "version"}),
    "brew": frozenset({"list", "info", "search", "config"}),
    "pip": frozenset({"list", "show", "freeze"}),
    "pip3": frozenset({"list", "show", "freeze"}),
    "npm": frozenset({"list", "ls", "outdated", "view"}),
}

# 解释器与构建工具 = 任意代码执行，默认不在白名单里。老板可以按机器
# 逐个放行（--allow-command），但放行后仍然拦内联代码与重定向越界。
NEVER_INLINE = ("python", "python3", "node", "ruby", "php", "perl", "deno", "bun")
INLINE_CODE_FLAGS = ("-c", "-e", "--eval", "--command", "--exec", "-p", "--print")

# 只读工具里少数"看着只读其实会写"的参数。
WRITE_FLAGS = {
    "sed": frozenset({"-i", "--in-place"}),
    "find": frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                       "-fprint", "-fprint0", "-fls", "-fprintf"}),
    "git": frozenset({"-p"}),  # git -p 不写，但 -c 传配置会执行 hook
}


def _norm(path: str | Path, base: str | Path | None = None) -> str:
    """归一化后再比对，否则 `~/.ssh/../.ssh/id_rsa`、`/etc//passwd` 会绕过片段匹配。

    `base` 是相对路径的解析基准：shell 命令里 `> out.txt` 落在**命令的工作目录**，
    不是 sidecar 进程的目录。护栏必须按同一个基准算，否则会判错方向——
    该拦的（真的写到根外）放过去，不该拦的（在根内）被拒。
    """
    p = Path(str(path)).expanduser()
    if base is not None and not p.is_absolute():
        p = Path(str(base)).expanduser() / p
    try:
        p = Path(os.path.abspath(str(p)))
    except (OSError, ValueError):
        return str(p)
    return str(p).lower().replace("\\", "/")


def path_denied(path: str | Path, cwd: str | Path | None = None) -> str | None:
    """命中凭据/系统目录或密钥文件名时返回原因，否则 None。"""
    norm = _norm(path, cwd)
    for frag in DENY_PATH_FRAGMENTS:
        if frag in norm or norm == frag.rstrip("/"):
            return (f"路径 {path} 属于本机凭据或系统目录（{frag}），"
                    "这台设备不开放")
    for frag in DENY_NAME_FRAGMENTS:
        if frag in norm or norm.endswith(frag.lstrip("/")):
            return (f"路径 {path} 命中密钥/版本库红条（{frag}），"
                    "这台设备不开放")
    return None


def path_writable(path: str | Path, allow_roots: list[str],
                  cwd: str | Path | None = None) -> str | None:
    """写操作必须落在用户显式给出的可写根目录下。

    没配 `--allow-root` 时 allow_roots 为空 = 什么都能读但什么都不能写。
    默认拒绝是对的：老板在页面上点「让他改一下那个文件」时，
    他授权的是「改那个文件」，不是「这台机器随便改」。
    """
    denied = path_denied(path, cwd)
    if denied:
        return denied
    norm = _norm(path, cwd)
    for root in allow_roots:
        base = _norm(root)
        if norm == base or norm.startswith(base + "/"):
            return None
    if not allow_roots:
        return (f"路径 {path} 不能写：这台设备还没开放任何可写目录。"
                "请在启动 sidecar 时加 --allow-root <目录>")
    return (f"路径 {path} 不在本机允许写入的目录里。已允许："
            + "、".join(allow_roots))


def _tokenize(command: str) -> list[str]:
    """分词，并把 `; & |` 一律切出来。

    默认的 `shlex.split` 不把连接符当分隔符：`ls; rm -rf x` 里 `ls;` 会粘成
    一个词，`_segments` 看不到 `;`，只能靠"白名单里没有 ls;"碰巧拒绝。
    开启 `punctuation_chars` 后它们总是独立 token，判定与 shell 的解析对齐。
    `2>&1` 里的 `&` 是文件描述符复制、不是后台分隔符，粘回去再切段。
    """
    lex = shlex.shlex(command, posix=(os.name != "nt"), punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    tokens = list(lex)
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        tok = tokens[index]
        if (tok == "&" and merged and merged[-1].endswith(">")
                and index + 1 < len(tokens) and tokens[index + 1].isdigit()):
            merged[-1] += "&" + tokens[index + 1]
            index += 2
            continue
        merged.append(tok)
        index += 1
    return merged


def _segments(tokens: list[str]) -> list[list[str]]:
    """按 shell 连接符切成若干段，逐段判首命令。

    单个 `&`（后台执行）也是命令分隔符：`ls & rm -rf x` 在 shell 里是两条
    命令，不切就会只判 `ls` 而把第二条放过去。换行/回车不在此列——它们在
    check_command 入口直接拒绝（多行命令无法用一次静态判定表达意图）。
    """
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok in ("&&", "||", ";", "|", "&"):
            if out[-1]:
                out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _reject_write_flags(binary: str, tokens: list[str]) -> str | None:
    bad = WRITE_FLAGS.get(binary)
    if not bad:
        return None
    for tok in tokens:
        stem = tok.split("=")[0]
        if stem in bad or (binary == "sed" and stem.startswith("-i")
                           and stem != "-n"):
            return (f"{binary} {tok} 会写文件/起进程。这台设备的 shell 只放行"
                    "只读用法，改文件请走 write_file 或 apply_patch（有 diff 和备份）")
    return None


def _check_segment(segment: list[str], allow_roots: list[str],
                   extra: set[str], cwd: str | Path) -> str | None:
    binary = os.path.basename(segment[0]).lower()
    rest = segment[1:]
    subs = READ_ONLY_SUBCOMMANDS.get(binary)
    if subs:
        first = next((t for t in rest if not t.startswith("-")), "")
        if first and first not in subs:
            return (f"{binary} {first} 不是只读子命令。本机只允许："
                    + "、".join(sorted(subs)))
    write_reason = _reject_write_flags(binary, rest)
    if write_reason:
        return write_reason
    if binary in NEVER_INLINE and any(t in INLINE_CODE_FLAGS for t in rest):
        return (f"{binary} 的内联代码（{next(t for t in rest if t in INLINE_CODE_FLAGS)}）"
                "等价于任意代码执行，会绕开本机护栏，一律拒绝")
    if binary not in READ_ONLY_COMMANDS | extra:
        return (f"{binary!r} 不在本机只读命令白名单里。要放行请在这台机器上"
                f"带 --allow-command {binary} 启动 sidecar")
    for index, tok in enumerate(rest):
        if tok in (">", ">>", ">!") or tok.rstrip("012").endswith(">") and len(tok) <= 3:
            target = rest[index + 1] if index + 1 < len(rest) else ""
            if target:
                reason = path_writable(target, allow_roots, cwd)
                if reason:
                    return f"命令输出重定向被拦：{reason}"
    return None


def check_command(command: str, allow_roots: list[str],
                  extra_commands: list[str] | None = None,
                  allow_destructive: bool = False,
                  cwd: str | Path | None = None) -> str | None:
    """命令能不能在这台机器上跑。返回 None=放行，否则返回拒绝原因。

    判断基于 `shlex` 分词，与 shell 真实解析并非逐字等价。这是刻意的：
    凡是分不出意图的写法（引号里藏 `;`、命令替换）一律拒绝，
    宁可让老板换个写法重来，也不能"看着像只读"就放行。

    `cwd` 必须和真正执行时用的工作目录一致：`ls > out.txt` 里的 `out.txt`
    落在哪儿只有 shell 说了算，护栏按别的目录算就会判反。
    """
    if not command.strip():
        return "命令为空"
    # 换行/回车在 shell 里就是命令分隔符，而执行走 shell=True：不拒的话
    # `ls\nrm -rf x` 只会判到第一段，第二条照样跑。与本机执行器
    # （app/computer_real.py 的 _SHELL_METACHARS 含 \n\r）同一口径。
    if "\n" in command or "\r" in command:
        return ("命令里有换行，等于一次下发多条命令，本机护栏无法逐条判定。"
                "请拆成多次下发；要改文件走 write_file / apply_patch（有 diff 和备份）")
    lowered = command.lower()
    if not allow_destructive:
        for frag in DESTRUCTIVE_FRAGMENTS:
            if frag in lowered:
                return (f"命令含破坏性片段 {frag!r}，本机默认拒绝。"
                        "确实需要时在这台机器上带 --allow-destructive 启动 sidecar")
    for mark in SUBSTITUTION_MARKS:
        if mark in command:
            return (f"命令里有 {mark} 这类替换/展开，本机无法静态判断它会做什么，"
                    "拒绝执行")
    try:
        tokens = _tokenize(command)
    except ValueError as exc:
        return f"命令无法解析：{exc}"
    extra = {c.strip().lower() for c in (extra_commands or []) if c.strip()}
    base = str(cwd) if cwd else os.getcwd()
    for segment in _segments(tokens):
        reason = _check_segment(segment, allow_roots, extra, base)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------- 配置与 HTTP


class SidecarError(RuntimeError):
    pass


def say(message: str) -> None:
    """进度只写 stdout——sidecar 通常挂在终端里看。"""
    print(message, flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit("这台机器还没接入：先跑 "
                         "`python3 aieos_sidecar.py enroll --server … --code …`")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def _api(server: str, path: str) -> str:
    base = server.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base + path


def request_json(server: str, path: str, payload: dict,
                 secret: str | None = None, timeout: float = 30.0) -> dict:
    """POST JSON。服务端把拒绝原因写在 HTTP 状态的 detail 里，原样带回来。"""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-AIEOS-Device-Key"] = secret
    req = urllib.request.Request(_api(server, path), data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("detail") or raw
        except json.JSONDecodeError:
            detail = raw[:200]
        raise SidecarError(f"HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise SidecarError(f"连不上服务端：{exc.reason}") from exc
    except TimeoutError:
        raise SidecarError("服务端响应超时") from None


def device_facts() -> dict:
    return {
        "platform": sys.platform,
        "hostname": platform.node() or "unknown",
        "agent_version": AGENT_VERSION,
        "capabilities": list(CORE_CAPABILITIES),
    }


# ---------------------------------------------------------------- 执行


def _seconds_left(deadline: str | None) -> float | None:
    """命令在服务端还有多少秒判过期。sidecar 不该跑一条注定被丢掉的长命令。"""
    if not deadline:
        return None
    try:
        due = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (due - datetime.now(timezone.utc)).total_seconds()


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（输出 {len(text)} 字，已截断）"


def default_workdir(cfg: dict) -> str:
    """没显式给 cwd 时命令在哪跑：优先第一个可写根目录，其次 sidecar 的当前目录。

    护栏算相对路径用的是同一个目录——两边不一致时，`ls > out.txt` 这种写法
    要么被误拦，要么更糟：被放到一个没打算开放的位置。
    """
    roots = cfg.get("allow_roots") or []
    return str(roots[0]) if roots else os.getcwd()


def resolve_path(raw: str, cfg: dict) -> Path:
    """文件操作里的相对路径按 `default_workdir` 算，和 shell 命令同一个基准。

    之前只 expanduser：老板在页面上写「线上问题清单.md」，sidecar 拿**自己的进程目录**
    去找，回一句「文件不存在：线上问题清单.md」——那个相对路径在他机器上指向哪，
    他根本不知道；同一个路径在 run_shell 里却是能用的。
    """
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else Path(default_workdir(cfg)) / path


def run_shell(params: dict, cfg: dict) -> dict:
    command = str(params.get("command") or params.get("cmd") or "")
    cwd = str(params.get("cwd") or default_workdir(cfg))
    reason = path_denied(cwd)
    if reason:
        raise PermissionError(f"命令工作目录不可用：{reason}")
    denied = check_command(command, cfg.get("allow_roots") or [],
                           cfg.get("allow_commands") or [],
                           bool(cfg.get("allow_destructive")), cwd=cwd)
    if denied:
        raise PermissionError(denied)
    timeout = SHELL_TIMEOUT_SECONDS
    left = _seconds_left(params.get("_deadline"))
    if left is not None:
        timeout = max(1.0, min(SHELL_TIMEOUT_SECONDS, left))
    started = time.monotonic()
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": command,
                "error": f"命令超过 {int(timeout)} 秒未结束，已终止"}
    return {"ok": proc.returncode == 0, "command": command, "cwd": cwd,
            "exit_code": proc.returncode,
            "stdout": _truncate(proc.stdout or ""),
            "stderr": _truncate(proc.stderr or ""),
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _read_lines(path: Path) -> list[str]:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"{path} 看起来是二进制文件，sidecar 只按文本处理")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} 不是 UTF-8 文本（{exc.reason}），"
                         "拒绝按文本改写") from exc
    return text.splitlines(keepends=True)


def read_file(params: dict, cfg: dict) -> dict:
    raw = str(params.get("path") or "")
    if not raw.strip():
        raise ValueError("path 不能为空")
    path = resolve_path(raw, cfg)
    denied = path_denied(path)
    if denied:
        raise PermissionError(denied)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in path.iterdir() if not p.name.startswith("."))
        return {"ok": True, "path": str(path), "is_dir": True,
                "entries": entries[:500], "count": len(entries)}
    lines = _read_lines(path)
    offset = max(0, int(params.get("offset") or 0))
    limit = min(int(params.get("limit") or 400), 2000)
    return {"ok": True, "path": str(path), "total_lines": len(lines),
            "offset": offset, "lines": lines[offset:offset + limit],
            "truncated": offset + limit < len(lines)}


def _backup(path: Path) -> str:
    """备份放在用户目录里，不污染仓库；同名冲突用路径哈希区分。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    target = BACKUP_DIR / f"{path.name}.{stamp}.{digest}.bak"
    shutil.copy2(path, target)
    return str(target)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".aieos-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _diff(old: str, new: str, path: Path) -> dict:
    diff = list(difflib.unified_diff(
        old.splitlines(keepends=True) or [""],
        new.splitlines(keepends=True) or [""],
        fromfile=f"a/{path.name}", tofile=f"b/{path.name}"))
    text = "".join(diff)
    return {"diff": text[:MAX_DIFF_CHARS],
            "diff_truncated": len(text) > MAX_DIFF_CHARS,
            "added_lines": sum(1 for line in diff
                               if line.startswith("+") and not line.startswith("+++")),
            "removed_lines": sum(1 for line in diff
                                 if line.startswith("-") and not line.startswith("---")),
            "hunks": sum(1 for line in diff if line.startswith("@@"))}


def _apply_edits(text: str, edits: list[dict]) -> tuple[str, list[str]]:
    """逐处替换锚点。任何一处失败就整批拒绝——不留「改了 3 处第 4 处没改」的半成品。"""
    changed: list[str] = []
    for index, edit in enumerate(edits, start=1):
        old = edit.get("old") if isinstance(edit, dict) else None
        new = edit.get("new") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError(f"第 {index} 处 edits 不合法：old 必须是非空原文字符串，"
                             "new 必须是字符串（删除传空串）")
        hits = text.count(old)
        if hits == 0:
            raise ValueError(f"第 {index} 处锚点在文件里找不到，整批改动已取消")
        replace_all = bool(edit.get("replace_all"))
        if hits > 1 and not replace_all:
            raise ValueError(f"第 {index} 处锚点在文件里出现 {hits} 次，"
                             "请把锚点扩到唯一，或显式传 replace_all=true")
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        changed.append(f"第 {index} 处（匹配 {hits} 次）")
    return text, changed


def write_file(params: dict, cfg: dict) -> dict:
    raw = str(params.get("path") or "")
    if not raw.strip():
        raise ValueError("path 不能为空")
    path = resolve_path(raw, cfg)
    reason = path_writable(path, cfg.get("allow_roots") or [])
    if reason:
        raise PermissionError(reason)
    content = params.get("content")
    if not isinstance(content, str):
        raise ValueError('write_file 需要 content（字符串，清空文件传 ""）')
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if params.get("dry_run"):
        return {"ok": True, "path": str(path), "dry_run": True,
                **_diff(old, content, path)}
    backup = _backup(path) if path.exists() else None
    _write_atomic(path, content)
    return {"ok": True, "path": str(path), "created": not old, "backup": backup,
            "bytes": len(content.encode()), **_diff(old, content, path)}


def apply_patch(params: dict, cfg: dict) -> dict:
    raw = str(params.get("path") or "")
    if not raw.strip():
        raise ValueError("path 不能为空")
    path = resolve_path(raw, cfg)
    reason = path_writable(path, cfg.get("allow_roots") or [])
    if reason:
        raise PermissionError(reason)
    edits = params.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError('apply_patch 需要 edits：[{"old": "原文", "new": "新文"}]')
    if not path.exists():
        raise FileNotFoundError(f"文件不存在，补丁没有可改的原件：{path}")
    old_text = "".join(_read_lines(path))
    new_text, changed = _apply_edits(old_text, edits)
    if params.get("dry_run"):
        return {"ok": True, "path": str(path), "dry_run": True,
                "edits_applied": changed, **_diff(old_text, new_text, path)}
    backup = _backup(path)
    _write_atomic(path, new_text)
    return {"ok": True, "path": str(path), "backup": backup,
            "edits_applied": changed, **_diff(old_text, new_text, path)}


EXECUTORS = {"run_shell": run_shell, "read_file": read_file,
             "write_file": write_file, "apply_patch": apply_patch}


def execute(task: dict, cfg: dict) -> dict:
    capability = task.get("capability") or ""
    executor = EXECUTORS.get(capability)
    if executor is None:
        raise ValueError(f"这台设备不会执行 {capability!r}（只会："
                         + "、".join(sorted(EXECUTORS)) + "）")
    params = dict(task.get("params") or {})
    params["_deadline"] = task.get("deadline_at")
    result = executor(params, cfg)
    if result.get("ok"):
        result.pop("error", None)
    return result


# ---------------------------------------------------------------- 主循环


def _roots_or_exit(values: list[str]) -> list[str] | int:
    roots: list[str] = []
    for value in values:
        # 存绝对路径：相对写法（`--allow-root work`）等于把可写范围绑到
        # 「下次 `run` 从哪个目录启动」——换个工作目录启动，开放的就是另一个目录。
        root = os.path.abspath(str(Path(value).expanduser()))
        denied = path_denied(root)
        if denied:
            say(f"可写目录被拒：{denied}")
            return 1
        if not Path(root).exists():
            say(f"可写目录不存在，已忽略：{root}")
            continue
        roots.append(root)
    return roots


def cmd_enroll(args) -> int:
    facts = device_facts()
    roots = _roots_or_exit(args.allow_root or [])
    if isinstance(roots, int):
        return roots
    try:
        out = request_json(args.server, "/computer-devices/enroll", {
            "code": args.code, "platform": facts["platform"],
            "hostname": facts["hostname"], "agent_version": AGENT_VERSION,
            "capabilities": facts["capabilities"]}, timeout=30.0)
    except SidecarError as exc:
        say(f"接入失败：{exc}")
        say("接入码是一次性的：过期或已被用过，请回「设备操作台」点「重新生成接入码」。")
        return 1
    save_config({"server": args.server, "device_id": out["device_id"],
                 "name": out["name"], "secret": out["secret"],
                 "allow_roots": roots,
                 "allow_commands": sorted({c.lower() for c in (args.allow_command or [])}),
                 "allow_destructive": bool(args.allow_destructive)})
    say(f"已接入为「{out['name']}」（{facts['hostname']} / {facts['platform']}）")
    say("可写目录：" + ("、".join(roots) if roots
                      else "无——这台设备只读，写文件会被本地拒绝"))
    say(f"配置写入 {CONFIG_PATH}（0600）。启动常驻：python3 aieos_sidecar.py run")
    return 0


def cmd_run(args) -> int:
    cfg = load_config()
    if args.allow_root:
        roots = _roots_or_exit(args.allow_root)
        if isinstance(roots, int):
            return roots
        # 只对本次会话生效：把「临时放宽可写范围」写回配置文件，
        # 等于让下一次开机自动继承一个更宽的授权。
        cfg["allow_roots"] = roots
    if args.allow_command:
        cfg["allow_commands"] = sorted(
            {c.lower() for c in (cfg.get("allow_commands") or [])}
            | {c.lower() for c in args.allow_command})
    say(f"已连接 {cfg['server']}，设备「{cfg['name']}」。"
        f"可写：{'、'.join(cfg.get('allow_roots') or []) or '无'}。Ctrl-C 退出。")
    try:
        request_json(cfg["server"], "/computer-devices/tunnel/heartbeat",
                     device_facts(), secret=cfg["secret"], timeout=20.0)
    except SidecarError as exc:
        say(f"心跳失败（不影响领命令）：{exc}")
    while True:
        try:
            out = request_json(
                cfg["server"], f"/computer-devices/tunnel/claim?wait={int(args.wait)}",
                {}, secret=cfg["secret"], timeout=args.wait + 15.0)
        except SidecarError as exc:
            say(f"领取失败：{exc}（10 秒后重试；密钥被吊销也是这个报错）")
            time.sleep(10)
            continue
        task = out.get("task")
        if not task:
            continue
        say(f"收到命令：{_task_summary(task)}")
        try:
            result = execute(task, cfg)
            ok = bool(result.get("ok"))
            error = None if ok else (result.get("error") or "本机执行未成功")
        except (PermissionError, ValueError, FileNotFoundError) as exc:
            # 本机护栏拦下也算"执行过了，结果是拒绝"——回报 ok=False，
            # 让老板在服务端审计里看得见真实原因，而不是默默丢命令。
            result = {"ok": False, "denied_locally": True, "error": str(exc)}
            ok, error = False, str(exc)
        except OSError as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok, error = False, result["error"]
        try:
            request_json(cfg["server"], "/computer-devices/tunnel/report",
                         {"dispatch_id": task["dispatch_id"], "ok": ok,
                          "result": result, "error": error},
                         secret=cfg["secret"], timeout=30.0)
            say(f"已回报：{'成功' if ok else f'失败/被拦 —— {error}'}")
        except SidecarError as exc:
            say(f"回报失败：{exc}（结果丢失，服务端会把它判成未回报）")


def _task_summary(task: dict) -> str:
    params = task.get("params") or {}
    key = "command" if task.get("capability") == "run_shell" else "path"
    return f"{task.get('capability')} {str(params.get(key) or '')[:120]}".strip()


def cmd_status(args) -> int:
    if not CONFIG_PATH.exists():
        say(f"{CONFIG_PATH} 不存在：这台机器还没接入")
        return 1
    cfg = load_config()
    facts = device_facts()
    try:
        out = request_json(cfg["server"], "/computer-devices/tunnel/heartbeat",
                           facts, secret=cfg["secret"], timeout=15.0)
    except SidecarError as exc:
        say(f"服务端说：{exc}")
        return 1
    say(f"设备「{cfg['name']}」在线：{out.get('online')}，能力："
        + "、".join(facts["capabilities"]))
    say("可写目录：" + ("、".join(cfg.get("allow_roots") or []) or "无（只读设备）"))
    say("额外放行的命令：" + ("、".join(cfg.get("allow_commands") or []) or "无"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AIEOS 设备端 sidecar（在本机执行 AI 员工领取的电脑操作）")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="用一次性接入码换出本机设备密钥")
    e.add_argument("--server", required=True,
                   help="后端地址，如 http://192.168.1.20:8000")
    e.add_argument("--code", required=True, help="设备操作台上生成的一次性接入码")
    e.add_argument("--allow-root", action="append", default=[],
                   help="允许写文件的目录，可重复。不给就是只读设备")
    e.add_argument("--allow-command", action="append", default=[],
                   help="在只读白名单之外额外放行的命令，可重复")
    e.add_argument("--allow-destructive", action="store_true",
                   help="允许含破坏性片段的命令（默认拒绝）")
    e.set_defaults(func=cmd_enroll)

    r = sub.add_parser("run", help="领命令、本机过护栏、执行、回报")
    r.add_argument("--allow-root", action="append", default=[],
                   help="本次会话的可写目录（覆盖配置，不写回）")
    r.add_argument("--allow-command", action="append", default=[],
                   help="本次会话额外放行的命令（写回配置）")
    r.add_argument("--wait", type=float, default=CLAIM_WAIT_SECONDS,
                   help="服务端替这台设备等待命令的秒数（<=30）")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="测试连通性并报告本机能力")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("已退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
