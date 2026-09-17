"""文件访问沙箱：把"能读哪个文件"从 LLM 手里收回到代码手里。

## 背景（为什么必须存在）

改造前 `FileReaderSkill` 的实现只有一行：

    path = Path(input_data["path"])
    content = path.read_text(...)

而 `input_data["path"]` 由 **LLM 生成的工具调用参数**决定。这意味着：
一次成功的提示注入就能让智能体读走 `apps/api/.env`（内含 JWT_SECRET、
模型 API Key），再通过 web_search 或聊天回复外带出去。
同时 `computer_run_shell` 的白名单以 `cat ` 开头即可通过，
同样可以 `cat .env`——两条路都指向同一份密钥。

## 设计原则

1. **默认拒绝**：只允许读取显式声明的沙箱根目录内的文件，其余一律拒绝。
   白名单而非黑名单——黑名单永远列不全。
2. **先解析再判断**：用 `Path.resolve()` 消解 `..` 和符号链接，
   防止 `storage/../../.env` 这类绕过。
3. **敏感文件二次拦截**：即使落在沙箱内，`.env` / 私钥 / `.ssh` 等
   也一律拒绝，避免"沙箱目录里恰好放了一份密钥"的情况。
4. **拒绝时说明原因**：返回明确错误，不静默降级——
   调用方（和排障的人）必须知道"为什么没读到"，而不是拿到空内容。
"""
from __future__ import annotations

import os
from pathlib import Path

# app/core/sandbox.py → app/core → app → apps/api → apps → 仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[4]

# 文件名级敏感清单（不区分大小写比较）
_SENSITIVE_FILE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".pgpass", ".htpasswd", "credentials", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml",
}

# 后缀级敏感清单
_SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".asc", ".gpg",
}

# 路径中包含这些目录名即拒绝（密钥/凭据目录）。
# 注意：只对"沙箱根之内的相对部分"生效——否则仓库本身位于
# /Users/xxx/.config/... 这类目录时会全盘误杀。
_SENSITIVE_DIR_PARTS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".docker",
}

# 系统级敏感文件（绝对路径前缀）
_SENSITIVE_ABS_PREFIXES = (
    "/etc/passwd", "/etc/shadow", "/etc/hosts", "/etc/ssh",
    "/private/etc", "/var/db",
)


class SandboxViolation(PermissionError):
    """路径越界或命中敏感规则。"""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


def sandbox_roots() -> list[Path]:
    """允许访问的根目录列表。

    - storage 根（Agent 交付文件夹、上传资料、知识库文件都在这下面）
    - 截屏目录（computer agent 的产物）
    """
    roots: list[Path] = []

    # storage：与 doc_generator.storage_root() 保持一致
    try:
        from app.core.config import settings

        storage = Path(settings.storage_dir)
        if not storage.is_absolute():
            storage = PROJECT_ROOT / settings.storage_dir
        roots.append(storage)
    except Exception:  # pragma: no cover - 配置异常时退化为仓库 storage
        roots.append(PROJECT_ROOT / "storage")

    roots.append(PROJECT_ROOT / "storage")
    roots.append(PROJECT_ROOT / "data" / "uploads")
    # computer agent 截屏输出目录
    roots.append(Path("/tmp/ai-eos-screens"))

    # 去重 + 解析，确保比较时是同一个基准
    seen: list[Path] = []
    for r in roots:
        try:
            resolved = r.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _owning_root(path: Path) -> Path | None:
    for root in sandbox_roots():
        if path == root or root in path.parents:
            return root
    return None


def _is_sensitive(path: Path) -> str | None:
    """命中敏感规则则返回原因，否则 None。

    目录名检查只针对相对沙箱根的部分，避免仓库自身路径被误判。
    """
    name_lower = path.name.lower()
    if name_lower in _SENSITIVE_FILE_NAMES or name_lower.startswith(".env"):
        return f"文件名 {path.name} 属于敏感配置"
    if path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return f"后缀 {path.suffix} 属于密钥类文件"

    root = _owning_root(path)
    if root is not None:
        relative_parts = path.relative_to(root).parts
        for part in relative_parts:
            if part.lower() in _SENSITIVE_DIR_PARTS:
                return f"路径包含敏感目录 {part}"

    path_str = str(path)
    for prefix in _SENSITIVE_ABS_PREFIXES:
        if path_str == prefix or path_str.startswith(prefix + "/"):
            return f"系统敏感路径 {prefix}"
    return None


def resolve_safe_path(raw_path: str, *, must_exist: bool = True) -> Path:
    """把用户/LLM 提供的路径解析为沙箱内的安全绝对路径。

    任何一步不满足就抛 SandboxViolation（调用方应转成用户可读的错误），
    绝不"尽量猜一个能用的路径"。
    """
    if not raw_path or not str(raw_path).strip():
        raise SandboxViolation("路径不能为空")

    candidate = Path(str(raw_path).strip())

    # 相对路径一律相对仓库根解析，避免受进程工作目录影响
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise SandboxViolation(f"路径无法解析：{exc}", path=str(candidate)) from exc

    roots = sandbox_roots()
    inside = any(resolved == r or r in resolved.parents for r in roots)
    if not inside:
        allowed = "、".join(str(r) for r in roots)
        raise SandboxViolation(
            f"路径不在允许访问的目录内。允许的根目录：{allowed}",
            path=str(resolved))

    reason = _is_sensitive(resolved)
    if reason:
        raise SandboxViolation(f"拒绝访问：{reason}", path=str(resolved))

    if must_exist and not resolved.is_file():
        raise SandboxViolation("文件不存在或不是普通文件", path=str(resolved))

    return resolved


def is_path_allowed(raw_path: str) -> tuple[bool, str]:
    """供 shell 白名单等场景做"仅判断、不抛异常"的检查。"""
    try:
        resolve_safe_path(raw_path, must_exist=False)
        return True, ""
    except SandboxViolation as exc:
        return False, str(exc)


def extract_path_tokens(command: str) -> list[str]:
    """从 shell 命令里粗提取疑似文件路径的 token，用于逐条沙箱校验。

    提取策略偏保守：只要 token 里有 `/` 或 `.` 就当作疑似路径交给沙箱判断，
    误判成路径只会导致"拒绝执行"，不会导致"放行危险操作"——失败方向是对的。
    """
    tokens: list[str] = []
    for chunk in command.replace("\n", " ").split():
        token = chunk.strip().strip("'\"")
        if not token or token.startswith("-"):
            continue
        if "/" in token or token.startswith("."):
            tokens.append(token)
    return tokens


def env_file_paths() -> list[Path]:
    """需要保护的 .env 实际位置（供启动自检提示用）。"""
    return [p for p in (PROJECT_ROOT / ".env", PROJECT_ROOT / "apps" / "api" / ".env")
            if p.is_file()]


def describe_sandbox() -> dict:
    """沙箱当前配置的可读描述（启动日志/自检接口用）。"""
    return {
        "project_root": str(PROJECT_ROOT),
        "allowed_roots": [str(p) for p in sandbox_roots()],
        "protected_files": [str(p) for p in env_file_paths()],
        "cwd": os.getcwd(),
    }
