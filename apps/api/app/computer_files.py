"""文件级本机操作：读文件 / 写文件 / 打补丁。

## 为什么单独一路

「让 AI 改代码」过去只有两个出口，都不够用：

- `run_shell` 只放行只读命令白名单，改不了任何文件；
- `run_write` 只允许 `cp/mv/mkdir/touch/rm`，能删能建，唯独不能写内容。

于是模型想改一行代码，只能 `type_text` 敲进编辑器、或者截图找坐标——
既不成立，也不安全。这一路补上三件事：读内容、整写文件、按锚点打补丁，
并且每次写盘前自动留备份、返回 unified diff，老板在审批单里看到的就是
"哪几个文件、改哪几行"，而不是一句"我要优化一下代码"。

## 安全姿态

1. **路径全盘可达，红线写在代码里**：`~/.ssh`、`/etc`、`.env`、`*.pem`
   这类路径直接硬拦截（复用 `computer_policy` 的同一份清单），
   其余目录允许——写操作仍要过三档审批，这是「允许全盘 + 三档审批」那条决定。
2. **宁可报错也不猜**：补丁锚点找不到、找到多处、文件不是 UTF-8 文本，
   一律整体放弃并说明原因，绝不"尽量改一部分"。
3. **不做静默降级**：读二进制/超长文件时如实标注截断，不伪造完整内容。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import uuid
import shutil
from datetime import datetime
from pathlib import Path

from app.core.sandbox import PROJECT_ROOT
from app.services import computer_policy as policy_service


class FileOpError(ValueError):
    """路径或参数不合法：转成模型看得懂的中文反馈，不静默返回空内容。"""


# 读：单次返回给模型的正文上限。再大就先让模型缩小范围，
# 一屏 2000 行的文件直接塞进上下文是纯粹的 token 浪费。
DEFAULT_MAX_READ_BYTES = 60_000
HARD_MAX_READ_BYTES = 200_000
# 写：单文件上限。超过它说明模型大概率想把整个仓库塞进一个调用。
MAX_WRITE_BYTES = 1_000_000
MAX_EDITS = 30
# diff 只是给人看的，控制在一次审批单能装下的篇幅
MAX_DIFF_CHARS = 20_000

_BIN_PROBE_BYTES = 8192


def _backup_root() -> Path:
    """备份目录：storage 下固定子目录。测试会把它指到 tmp。"""
    from app.core.config import settings

    root = Path(settings.storage_dir)
    if not root.is_absolute():
        root = PROJECT_ROOT / settings.storage_dir
    return (root / "computer-file-backups").resolve()


def _resolve_path(raw: object, *, must_exist: bool) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise FileOpError("path 不能为空")
    candidate = Path(os.path.expanduser(text))
    if not candidate.is_absolute():
        # 相对路径一律相对仓库根，避免受后端进程工作目录影响
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise FileOpError(f"路径无法解析：{exc}") from exc

    block = policy_service.hard_block_path_reason(str(resolved))
    if block:
        raise FileOpError(f"{block}（{resolved}）")

    if resolved.is_dir():
        raise FileOpError(f"{resolved} 是目录，不是文件")
    if must_exist and not resolved.is_file():
        raise FileOpError(f"文件不存在：{resolved}")
    return resolved


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BIN_PROBE_BYTES]


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FileOpError(f"读取失败：{exc}") from exc


def _decode_strict(data: bytes, path: Path) -> str:
    """写回前必须能原样解码——用 errors=replace 读进来再写出去就是毁文件。"""
    try:
        # newline="" 等价于不做换行归一化：CRLF 文件改完还是 CRLF
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileOpError(
            f"{path} 不是 UTF-8 文本（{exc.reason}），"
            "拒绝按文本改写；请改用受审批的 run_shell 走专门工具") from exc


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds")
    except OSError:
        return None


def _backup_path(target: Path, now: datetime) -> tuple[Path, Path]:
    """(备份目录, 备份文件名)。文件名带完整路径哈希，避免同名覆盖。"""
    digest = hashlib.sha256(str(target).encode()).hexdigest()[:10]
    safe = re.sub(r"[^\w.+-]", "_", target.name)[:60]
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return _backup_root(), Path(f"{safe}.{stamp}.{digest}.bak")


def _make_backup(target: Path) -> str | None:
    """覆盖前留一份原文件。备份失败就不写——「改坏了找不到原件」比"这次没改成"严重。"""
    try:
        backup_dir, backup_name = _backup_path(target, datetime.now())
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_dir / backup_name)
        return str((backup_dir / backup_name).resolve())
    except OSError as exc:
        raise FileOpError(f"备份失败，已放弃写入：{exc}") from exc


def _unified_diff(old: str, new: str, path: str) -> dict:
    before = old.splitlines(keepends=True)
    after = new.splitlines(keepends=True)
    raw = list(difflib.unified_diff(before, after,
                                   fromfile=f"a/{path}", tofile=f"b/{path}"))
    text = "".join(raw)
    truncated = len(text) > MAX_DIFF_CHARS
    return {"diff": text[:MAX_DIFF_CHARS], "diff_truncated": truncated,
            "hunks": sum(1 for line in raw if line.startswith("@@")),
            "added_lines": sum(1 for line in raw
                               if line.startswith("+") and not line.startswith("+++")),
            "removed_lines": sum(1 for line in raw
                                 if line.startswith("-") and not line.startswith("---"))}


def _write_atomic(target: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace：不留半个文件。"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 修复：临时名只按 pid——同进程两个协程并发写同一目标时，
        # 第二个 os.replace 会因 tmp 已被第一个消费而报错。加唯一后缀。
        tmp = target.parent / (f".{target.name}.aieos-{os.getpid()}"
                               f"-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
    except FileOpError:
        raise
    except OSError as exc:
        raise FileOpError(f"写入失败：{exc}") from exc


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

def _read_file_sync(params: dict) -> dict:
    path = _resolve_path(params.get("path"), must_exist=True)
    data = _read_bytes(path)
    size = len(data)

    max_bytes = params.get("max_bytes")
    max_bytes = DEFAULT_MAX_READ_BYTES if max_bytes in (None, "") else int(
        _bounded(max_bytes, 64, HARD_MAX_READ_BYTES, "max_bytes"))

    if _looks_binary(data):
        return {"ok": False, "path": str(path), "size": size, "binary": True,
                "error": f"{path.name} 是二进制文件（前 {size} 字节含 NUL），"
                         "无法按文本读取"}

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    offset = _bounded(params.get("offset"), 1, total_lines or 1, "offset",
                      default=1)
    limit = _bounded(params.get("limit"), 1, 5_000, "limit",
                     default=400)
    window = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(window)
    byte_cap = len(body.encode()) > max_bytes
    if byte_cap:
        body = body.encode()[:max_bytes].decode("utf-8", errors="replace")
        window = body.splitlines()

    return {"ok": True, "path": str(path), "size": size,
            "modified_at": _mtime(path), "total_lines": total_lines,
            "offset": offset, "limit": limit,
            "returned_lines": len(window),
            "has_more": offset - 1 + len(window) < total_lines,
            "content_truncated_by_bytes": byte_cap,
            "content": body}


def _bounded(raw: object, low: int, high: int, name: str,
             default: int | None = None) -> int:
    """把模型给的数字夹进合法区间；非数字时按 default，没 default 就报错。"""
    if raw in (None, ""):
        if default is None:
            raise FileOpError(f"{name} 不能为空")
        return default
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise FileOpError(f"{name} 必须是整数，收到 {raw!r}") from None
    return max(low, min(value, high))


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

def _write_file_sync(params: dict) -> dict:
    path = _resolve_path(params.get("path"), must_exist=False)
    content = params.get("content")
    if not isinstance(content, str):
        raise FileOpError("content 必须是字符串（要清空文件就传空串 \"\"）")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise FileOpError(
            f"内容 {len(data)} 字节，超过单文件 {MAX_WRITE_BYTES} 字节上限；"
            "请拆分或改用 apply_patch 只改必要片段")

    existed = path.is_file()
    old = _decode_strict(_read_bytes(path), path) if existed else ""
    changed = old != content

    stats = _unified_diff(old, content, str(path))
    result = {"ok": True, "path": str(path), "existed": existed,
              "bytes": len(data), "encoding": "utf-8", **stats}

    if params.get("dry_run") is True:
        return {**result, "dry_run": True, "changed": changed,
                "message": ("预演：内容与现有文件不同，确认后会先备份再写盘"
                            if changed else "预演：内容与现有文件一致，不会写盘")}

    if not changed:
        return {**result, "dry_run": False, "changed": False,
                "backup_path": None,
                "message": "内容未变化，已跳过写盘（不产生备份）"}

    backup = _make_backup(path) if existed else None
    _write_atomic(path, data)
    return {**result, "dry_run": False, "changed": True,
            "backup_path": backup,
            "message": (f"已备份原件后写入 {len(data)} 字节" if backup
                        else f"新建文件，写入 {len(data)} 字节")}


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------

def _apply_patch_sync(params: dict) -> dict:
    path = _resolve_path(params.get("path"), must_exist=True)
    edits = params.get("edits")
    if not isinstance(edits, list) or not edits:
        raise FileOpError('edits 必须是非空数组：[{"old": "原文", "new": "新文"}]')
    if len(edits) > MAX_EDITS:
        raise FileOpError(f"一次最多 {MAX_EDITS} 处修改，收到 {len(edits)} 处")

    original = _decode_strict(_read_bytes(path), path)
    working = original
    applied: list[dict] = []

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise FileOpError(f"第 {index + 1} 处修改不是对象")
        old = edit.get("old")
        new = edit.get("new")
        if not isinstance(old, str) or not old:
            raise FileOpError(f"第 {index + 1} 处修改缺少 old（要替换的原文，不能为空）")
        if not isinstance(new, str):
            raise FileOpError(f"第 {index + 1} 处修改缺少 new（替换后的文本，可以是空串表示删除）")

        occurrences = working.count(old)
        replace_all = edit.get("replace_all") is True
        if occurrences == 0:
            raise FileOpError(
                f"第 {index + 1} 处修改定位失败：锚点在 {path.name} 里找不到。"
                "先用 read_file 核对原文（注意缩进和换行），"
                "或去掉这条改动后重发")
        if occurrences > 1 and not replace_all:
            raise FileOpError(
                f"第 {index + 1} 处修改有歧义：锚点匹配到 {occurrences} 处。"
                "请多带几行上下文让它唯一，或显式传 replace_all=true")
        working = working.replace(old, new, -1 if replace_all else 1)
        applied.append({"index": index, "occurrences": occurrences,
                        "replace_all": replace_all})

    stats = _unified_diff(original, working, str(path))
    result = {"ok": True, "path": str(path), "edits": applied,
              "edits_applied": len(applied), **stats}

    if params.get("dry_run") is True:
        return {**result, "dry_run": True,
                "changed": original != working,
                "message": "预演：确认后会先备份原件再落盘"}

    if original == working:
        return {**result, "dry_run": False, "changed": False,
                "backup_path": None, "message": "补丁没有产生任何变化，未写盘"}

    backup = _make_backup(path)
    _write_atomic(path, working.encode("utf-8"))
    return {**result, "dry_run": False, "changed": True, "backup_path": backup,
            "bytes": len(working.encode()),
            "message": f"已备份原件后应用 {len(applied)} 处修改"}


# 审批单要在建单时就带上「改哪几行」。这两个能力有内容可算，read_file 没有。
PREVIEW_CAPABILITIES = ("write_file", "apply_patch")
# 存进审批单的 diff 比给模型看的更短：待审批列表会一次拉出多条，
# 每条都塞 20K 字符的 diff 等于把审批页变成文件浏览器。
MAX_PREVIEW_DIFF_CHARS = 4_000


def preview_file_op(capability: str, params: dict) -> dict:
    """只演不写：算一次改动预览，供审批单展示。不写盘、不留备份。

    为什么必须在建单时算：老板若只看到 params 的 200 字截断 JSON
    （`write_file` 那串必然是被截断的文件正文），三档审批就退化成橡皮图章。
    预演失败（锚点找不到、文件不是 UTF-8）照样建单，失败原因如实进预览，
    而不是干脆不给看。
    """
    p = {**(params or {}), "dry_run": True}
    try:
        if capability == "write_file":
            result = _write_file_sync(p)
        elif capability == "apply_patch":
            result = _apply_patch_sync(p)
        else:
            return {"available": False,
                    "reason": f"{capability} 没有可预演的文件内容"}
    except (FileOpError, OSError) as exc:
        return {"available": False, "reason": str(exc),
                "path": str((params or {}).get("path") or "")}

    diff = str(result.get("diff") or "")
    return {
        "available": True,
        "path": result.get("path"),
        "changed": bool(result.get("changed")),
        # 新建文件没有「原件」可回滚，审批页要单独标出来
        "create": capability == "write_file" and result.get("existed") is False,
        "hunks": int(result.get("hunks") or 0),
        "added_lines": int(result.get("added_lines") or 0),
        "removed_lines": int(result.get("removed_lines") or 0),
        "edits": len(result.get("edits") or []),
        "diff": diff[:MAX_PREVIEW_DIFF_CHARS],
        "diff_truncated": (bool(result.get("diff_truncated"))
                           or len(diff) > MAX_PREVIEW_DIFF_CHARS),
    }


async def read_file(params: dict) -> dict:
    return await asyncio.to_thread(_read_file_sync, params or {})


async def write_file(params: dict) -> dict:
    return await asyncio.to_thread(_write_file_sync, params or {})


async def apply_patch(params: dict) -> dict:
    return await asyncio.to_thread(_apply_patch_sync, params or {})


FILE_EXECUTORS = {
    "read_file": read_file,
    "write_file": write_file,
    "apply_patch": apply_patch,
}
