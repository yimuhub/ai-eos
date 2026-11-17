"""TOTP 二次验证服务（V6.1.1 超级管理后台门禁升级）。

RFC 6238 标准 TOTP（pyotp 实现，Authenticator 全兼容）。三因子门禁：
超管身份 + 超级密钥（知道）+ TOTP 动态码（持有）。

状态文件 `totp_2fa.json`（跟随数据库目录，与 super_key.key 同位）：
{
  "secret": "<base32>",          // 绑定时生成
  "enabled": true,               // confirm 首码通过后置位
  "account": "deployer@ai-eos",  // otpauth 展示用
  "issuer": "AI-EOS",
  "recovery": ["<sha256>", ...], // 恢复码哈希（明文只在签发时出现一次）
  "created_at": "..."
}

逃生舱（丢手机）：部署者删掉 totp_2fa.json 重启 → 回到未绑定态重新扫码。
防爆破：连续 5 次验证失败 → 60 秒冷却（进程内计数，重启即清）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

import pyotp

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_FILE_NAME = "totp_2fa.json"
_MAX_ATTEMPTS = 5
_COOLDOWN_S = 60
_RECOVERY_COUNT = 8

_attempts: dict[str, list[float]] = {}  # 进程内失败时间窗（重启即清）


def _file_path() -> Path:
    """状态文件位置：跟随数据库目录（与 super_key.key 同一候选逻辑的首选）。"""
    from app.core.config import _secret_file_candidates
    candidates = _secret_file_candidates(settings.database_url,
                                         settings.storage_dir)
    if candidates:
        return candidates[0].parent / _FILE_NAME
    return Path("./data") / _FILE_NAME


def _read_state() -> dict | None:
    try:
        raw = _file_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("secret"):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass  # noqa: silent-except — 文件缺失/损坏=未绑定态，属预期分支（返回 None 由调用方处理）


def _write_state(state: dict) -> None:
    path = _file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def status() -> dict:
    state = _read_state()
    return {"enrolled": bool(state and state.get("enabled")),
            "pending": bool(state and not state.get("enabled")),
            "account": (state or {}).get("account"),
            "created_at": (state or {}).get("created_at")}


def enroll(account: str) -> str:
    """生成新 TOTP 密钥与 otpauth URI（旧绑定立即作废）。返回 URI 给前端渲染二维码。"""
    secret = pyotp.random_base32()
    account = (account or "deployer").strip()[:120] or "deployer"
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=account, issuer_name="AI-EOS")
    _write_state({"secret": secret, "enabled": False, "account": account,
                  "issuer": "AI-EOS",
                  "recovery": [],
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    logger.warning("[security] TOTP 二次验证已生成新密钥（等待首次确认绑定）。")
    return uri


def confirm_enrollment(code: str) -> dict:
    """首码确认：绑定生效 + 签发一次性恢复码（明文仅此一次）。"""
    state = _read_state()
    if state is None:
        raise ValueError("还没有生成 TOTP 密钥，请先调用 enroll")
    if not pyotp.TOTP(state["secret"]).verify(code, valid_window=1):
        raise PermissionError("验证码不匹配，绑定未生效")
    state["enabled"] = True
    plain = [secrets.token_hex(4) + "-" + secrets.token_hex(4)
             for _ in range(_RECOVERY_COUNT)]
    state["recovery"] = [
        hashlib.sha256(c.encode()).hexdigest() for c in plain]
    _write_state(state)
    logger.warning("[security] TOTP 二次验证绑定生效，已签发 %d 个恢复码。",
                   len(plain))
    return {"recovery_codes": plain}


def _cooldown_hit() -> int:
    """失败过密就冷却；返回剩余秒数（0 = 可验证）。"""
    now = time.time()
    window = [t for t in _attempts.get("fail", []) if now - t < _COOLDOWN_S]
    _attempts["fail"] = window
    if len(window) >= _MAX_ATTEMPTS:
        return int(_COOLDOWN_S - (now - window[0])) + 1
    return 0


def verify_code(code: str) -> bool:
    """校验 TOTP 动态码或恢复码（恢复码一次性）。失败计冷却。"""
    hit = _cooldown_hit()
    if hit:
        raise PermissionError(f"尝试次数过多，请 {hit} 秒后再试")
    state = _read_state()
    if state is None or not state.get("enabled"):
        raise PermissionError("TOTP 尚未绑定")

    code = (code or "").strip().replace(" ", "")
    totp = pyotp.TOTP(state["secret"])
    if totp.verify(code, valid_window=1):
        return True
    # 恢复码兜底：一次性（用完即从列表移除）
    h = hashlib.sha256(code.encode()).hexdigest()
    if h in (state.get("recovery") or []):
        state["recovery"] = [x for x in state["recovery"] if x != h]
        _write_state(state)
        logger.warning("[security] 恢复码已使用一个（剩余 %d 个）。",
                       len(state["recovery"]))
        return True
    _attempts.setdefault("fail", []).append(time.time())
    raise PermissionError("验证码不匹配")


def disable() -> None:
    """解除绑定（需超级密钥作为授权，由路由层把关）。"""
    path = _file_path()
    try:
        path.unlink()
    except OSError:  # noqa: silent-except — 文件不存在时 disable 也视为成功
        pass
    logger.warning("[security] TOTP 二次验证已解除绑定。")


def generate_otpauth_uri(account: str = "deployer") -> str:
    """仅用于文档/调试展示（正式签发走 enroll）。"""
    return pyotp.TOTP(pyotp.random_base32()).provisioning_uri(
        name=account, issuer_name="AI-EOS")
