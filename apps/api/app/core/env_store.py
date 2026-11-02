"""模型密钥的本机落盘：写 .env、立即生效、只回显掩码。

供应商与模型两条配置链共用这一份实现，避免「模型能改、供应商不能改」
或两处权限/白名单各写一套后漂移。
"""
from __future__ import annotations

import logging
import os
import re
import tempfile

from app.core.config import is_sensitive_env_name

logger = logging.getLogger(__name__)

PROVIDER_ENV_VARS: dict[str, str] = {
    "openai_compat": "OPENAI_COMPAT_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# 自定义供应商的密钥变量名：由服务端生成，不允许调用方任意指定。
# 形状固定，便于审计「哪些变量是模型密钥」。
PROVIDER_KEY_ENV_RE = re.compile(r"^AIEOS_PROVIDER_[A-Z0-9_]{2,60}_API_KEY$")


def provider_env_name(provider_type: str, slug: str) -> str:
    """为供应商生成稳定、可读的密钥变量名。"""
    raw = re.sub(r"[^A-Za-z0-9]+", "_", slug or "").strip("_").upper()
    if not raw:
        raw = (provider_type or "custom").upper()
    return f"AIEOS_PROVIDER_{raw[:48]}_API_KEY"


def is_allowed_model_env(env_name: str) -> bool:
    """模型密钥只允许写这三个默认变量或服务端生成的自定义供应商变量。"""
    if is_sensitive_env_name(env_name):
        return False
    return env_name in set(PROVIDER_ENV_VARS.values()) or bool(
        PROVIDER_KEY_ENV_RE.match(env_name))


def mask_key(key: str) -> str:
    """只回显足够让人确认"是不是填对了那一串"的片段。"""
    if len(key) <= 8:
        return "…" + key[-2:]
    return key[:4] + "…" + key[-4:]


def persist_env_key(env_name: str, api_key: str) -> None:
    """把密钥写进 .env 并立即生效（原子替换 + 600 权限）。

    * 只替换同名行，其余内容原样保留——.env 里还有 JWT_SECRET 等
      别的配置，整文件重写必须无损。
    * 先写临时文件再 os.replace，写到一半断电不会留下截断的 .env。
    * 同时写入 os.environ：`resolved_api_key` 是按调用时读取的，
      因此不需要重启后端，下一次任务就能用上新密钥。
    """
    from app.core.env_loader import env_file_path

    path = env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{env_name}={api_key}"

    kept: list[str] = []
    replaced = False
    if path.is_file():
        for old in path.read_text(encoding="utf-8").splitlines():
            probe = old.strip()
            if probe.startswith("export "):
                probe = probe[len("export "):].lstrip()
            if probe.split("=", 1)[0].strip() == env_name:
                if not replaced:
                    kept.append(line)
                    replaced = True
                continue  # 同名行只保留一条
            kept.append(old)
    if not replaced:
        kept.append(line)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            logger.warning("清理临时 .env 失败（%s）：%s", tmp, exc)
        raise

    os.environ[env_name] = api_key
