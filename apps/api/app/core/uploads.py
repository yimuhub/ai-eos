"""上传加固：流式落盘、大小上限、扩展名白名单、服务端生成文件名。

审查 S9：改造前两处上传都是 `data = await file.read()` + `write_bytes`，
即"整文件进内存"且没有任何大小上限，并发几个大文件即可打爆 API 进程；
`assets` 还把用户传入的 `file.filename` 直接拼进落盘路径（可穿越目录）。

本模块把三件事收口到一处，assets / kb2 共用：

1. 上限来自 `Settings.max_upload_mb`（默认 25MB），不散落硬编码；
2. 落盘按 1MB 块流式进行，边写边累计，超限立即删除半成品并抛 413；
3. 落盘文件名完全由服务端生成（uuid + 白名单扩展名），用户提供的
   原始文件名经过 basename/控制字符清洗后**只作为元数据**保存。

扩展名白名单只能挡住"文件名伪装"，挡不住"把可执行内容改成 .txt"。
真正的 MIME/magic bytes 校验尚未实现；二进制文档（PDF/DOCX）的解析
目前仍在 API 进程内进行，尚未隔离到受限子进程（审查 S9 的后续项）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 流式读取块大小：1MB。单块大小决定峰值内存，与总上限无关。
CHUNK_SIZE = 1024 * 1024

# 知识库文档：与 kb2 文本提取能力一致（.txt/.md/.csv/.json 直接解码，
# .pdf/.docx 走 pypdf/python-docx，.xlsx/.pptx 走 doc_convert）。
# 表格/演示与图片来自 AgentChat、会议、员工档案页的 accept 列表——
# 收紧白名单时不能把前端已经允许的格式挡掉，否则是"安全修复引入功能回归"。
KB_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".txt", ".pdf", ".docx", ".csv", ".json",
    ".xlsx", ".xls", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
})

# 资产中心：沿用 assets.EXT_TYPE 的现有分类，另补常见图片/音视频类型。
# 刻意不含 .svg/.html/.js 之外的脚本类可执行格式（.sh/.exe/.bat 等一律拒绝）。
ASSET_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # 文档（.xlsx/.pptx 是 Agent 常产出的交付物格式）
    ".md", ".txt", ".pdf", ".docx", ".csv", ".json", ".xlsx", ".pptx",
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    # 音视频
    ".mp3", ".wav", ".m4a", ".ogg", ".mp4", ".mov", ".webm",
    # 代码/数据（资产中心原本就允许，用于保存 Agent 产出的脚本与结构化数据）
    ".py", ".js", ".ts",
})

_MAX_NAME_LEN = 200


def max_upload_bytes() -> int:
    """当前生效的单文件上限（字节）。每次调用读取配置，便于测试与热改。"""
    try:
        mb = int(settings.max_upload_mb)
    except (TypeError, ValueError):
        mb = 25
    return max(1, mb) * 1024 * 1024


def safe_original_name(raw: str | None) -> str:
    """把用户传入的文件名清洗成"只能当元数据用"的形式。

    只取最后一段（同时按 / 与 \\ 切分）、去掉控制字符与不可打印字符。
    调用方**不得**把它拼进落盘路径——落盘名一律用 `generate_stored_name`。
    """
    text = str(raw or "").replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip().strip(".")
    return name[:_MAX_NAME_LEN] or "upload"


def extension_of(name: str) -> str:
    return Path(name).suffix.lower()


def validate_extension(raw_filename: str | None,
                       allowed: frozenset[str]) -> str:
    """白名单校验，返回小写扩展名；不在白名单抛 400。"""
    ext = extension_of(safe_original_name(raw_filename))
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext or '(无扩展名)'}；"
                   f"允许：{', '.join(sorted(allowed))}")
    return ext


def generate_stored_name(ext: str) -> str:
    """服务端生成落盘文件名：随机 uuid + 已校验的扩展名。"""
    return f"{uuid.uuid4().hex}{ext}"


def ensure_within(root: Path, target: Path) -> Path:
    """确认 target resolve() 后仍在 root 内，否则 400。

    `resolve()` 会展开符号链接，因此也能挡住"插件/上传目录里放个软链
    指向 /etc"这类越界写入。
    """
    root_resolved = root.resolve()
    resolved = target.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise HTTPException(status_code=400, detail="非法存储路径")
    return resolved


async def stream_upload(file: UploadFile, dest_dir: Path, dest_name: str,
                        max_bytes: int | None = None) -> tuple[Path, int]:
    """把上传流按块写进 dest_dir/dest_name，返回 (绝对路径, 字节数)。

    - 超过上限（默认取 Settings.max_upload_mb）：删除半成品并抛 413；
    - 任何异常（含客户端中断）都会删除半成品，不留残缺文件；
    - dest_name 必须由 `generate_stored_name()` 生成；这里再校验一次
      路径仍在 dest_dir 内，防止调用方误把用户输入传进来。
    """
    limit = max_upload_bytes() if max_bytes is None else max(1, int(max_bytes))

    # 客户端声明的大小可先快速拒绝，省掉一次完整落盘
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上限 {limit // (1024 * 1024)}MB")

    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = ensure_within(dest_dir, dest_dir / Path(dest_name).name)

    total = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过上限 {limit // (1024 * 1024)}MB")
                fh.write(chunk)
    except BaseException:
        # HTTPException / 客户端断开 / 磁盘错误：一律清掉半成品
        try:
            dest.unlink(missing_ok=True)
        except OSError as cleanup_err:
            # 半成品删不掉 → 磁盘上会残留不完整文件，必须留痕
            logger.warning("上传中断后删除半成品失败 path=%s: %s",
                           dest, cleanup_err)
        raise
    return dest, total
