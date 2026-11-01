"""离线 License 校验（E1）。

## 设计边界

* **完全离线**：只读本地文件 / 环境变量，不连任何授权服务器，也不上报
  使用数据。桌面版、内网部署都能用。
* **不发明商业参数**：本模块只负责"签名是否可信、是否过期、席位上限是多少"。
  价格、套餐、试用天数、超量策略都不在这里定义——License 只是把这些
  决策的结果（`seats` / `features` / `expires_at`）带过来。
* **未配置 = 社区模式**：`status="missing"`、`seats=None`（不限席位）、
  `features=[]`、`read_only=False`。升级到本版本但没买 License 的
  用户，行为与之前完全一致。

## License 格式

```json
{
  "payload": {
    "licensee": "某某公司",
    "instance": "install-001",
    "seats": 20,
    "features": ["usage", "billing"],
    "expires_at": "2027-12-31T23:59:59Z",
    "issued_at": "2026-01-01T00:00:00Z",
    "version": 1
  },
  "signature": "<base64 Ed25519 signature>"
}
```

签名原文 = `json.dumps(payload, sort_keys=True, separators=(",", ":"))`
（UTF-8，与示例完全一致；`ensure_ascii` 保持默认 True）。

读取优先级：

1. `AIEOS_LICENSE`：内联 JSON，或它的 base64（便于放进单行 env / 部署面板）；
2. `AIEOS_LICENSE_FILE`：License 文件路径，默认 `apps/api/license.json`；
3. 都没有 → 社区模式。

公钥来自 `AIEOS_LICENSE_PUBKEY`（base64 raw 32 字节，或 PEM）。
未配置时使用代码内嵌的**演示公钥**，`verification` 标为 `"demo-key"`，
用于让社区/试用环境也能验证由发行方演示私钥签出的 License。
演示私钥不在仓库里，也不随产品分发。

## 缓存

`get_state()` 按「License 原文指纹 + 公钥」缓存校验结果；每次调用只做
一次环境变量读取 / 文件 stat，不会重复解析签名。缓存读写由
`threading.Lock` 保护（FastAPI 的同步依赖与后台线程都可能调用）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.env_loader import API_ROOT
from app.core.logging_config import get_logger

logger = get_logger(__name__)

LICENSE_ENV = "AIEOS_LICENSE"
LICENSE_FILE_ENV = "AIEOS_LICENSE_FILE"
PUBKEY_ENV = "AIEOS_LICENSE_PUBKEY"
DEFAULT_LICENSE_FILE = API_ROOT / "license.json"

# 演示公钥（对应私钥只在发行方离线环境，仓库内没有、也不应存在）。
_DEMO_PUBKEY_B64 = "Z6fPprD758Vegp5hdLjCGpOj25M3mk1jH5EU/sRpJGc="

# 本版本认识的 feature 名。未知名称**不影响校验通过**，只记录并忽略：
# 这样发行方可以先卖"未来功能"，老版本收到后不会直接判定 License 无效。
# 这里刻意只放中性的能力名，不含任何价格/套餐语义。
KNOWN_FEATURES = frozenset({
    "seats",           # 席位上限生效
    "usage",           # 用量看板
    "billing",         # 账单导出
    "audit",           # 审计导出
    "sso",             # 单点登录（尚未实现，先收下标记）
    "custom_branding",  # 自定义品牌
})

STATUS_MISSING = "missing"
STATUS_INVALID = "invalid_signature"
STATUS_EXPIRED = "expired"
STATUS_VALID = "valid"

VERIFICATION_DEMO = "demo-key"
VERIFICATION_CONFIGURED = "configured-key"


class _LicenseFormatError(Exception):
    """License 结构本身不可解析（不携带原文，避免进入日志）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LicenseState:
    """一次校验的完整结果。所有字段都可安全序列化给前端（不含签名原文）。"""

    status: str = STATUS_MISSING
    seats: int | None = None
    features: tuple[str, ...] = ()
    ignored_features: tuple[str, ...] = ()
    expires_at: datetime | None = None
    issued_at: datetime | None = None
    licensee: str | None = None
    instance: str | None = None
    version: str | None = None
    verification: str = VERIFICATION_DEMO
    read_only: bool = False
    reason: str = ""
    source: str = "none"

    @property
    def is_licensed(self) -> bool:
        return self.status in (STATUS_VALID, STATUS_EXPIRED)

    @property
    def mode(self) -> str:
        """对外展示的模式：community / licensed / invalid。

        `expired` 仍属于 licensed（只是只读），由 `read_only` 单独表达。
        """
        if self.status == STATUS_MISSING:
            return "community"
        if self.status == STATUS_VALID or self.status == STATUS_EXPIRED:
            return "licensed"
        return "invalid"


def _as_text(value: Any, field_name: str, *, max_len: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _LicenseFormatError(f"{field_name}_not_string")
    return value.strip()[:max_len] or None


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    """解析 ISO 日期/时间；纯日期按当天 00:00:00Z 解释。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _LicenseFormatError(f"{field_name}_not_string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _LicenseFormatError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decode_envelope(raw: str) -> dict:
    """把内联 JSON / base64(JSON) 统一成 dict；失败抛 _LicenseFormatError。"""
    text = raw.strip()
    if not text:
        raise _LicenseFormatError("empty")
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise _LicenseFormatError("not_base64_json") from exc
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _LicenseFormatError("malformed_json") from exc
    if not isinstance(envelope, dict):
        raise _LicenseFormatError("envelope_not_object")
    return envelope


def canonical_payload(payload: dict) -> bytes:
    """签名原文（发行方与校验方必须使用同一份实现）。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_public_key(public_key_b64: str) -> Ed25519PublicKey:
    """接受 base64(raw 32B) 或 PEM；无效公钥抛 _LicenseFormatError。"""
    text = (public_key_b64 or "").strip()
    if not text:
        raise _LicenseFormatError("public_key_empty")
    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_public_key(text.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise _LicenseFormatError("public_key_pem_invalid") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise _LicenseFormatError("public_key_not_ed25519")
        return key
    try:
        raw = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise _LicenseFormatError("public_key_not_base64") from exc
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise _LicenseFormatError("public_key_length_invalid") from exc


def _configured_public_key() -> tuple[str, str]:
    """返回 (公钥串, verification 标记)。未配置 → 演示公钥。"""
    env_value = (os.environ.get(PUBKEY_ENV) or "").strip()
    if env_value:
        return env_value, VERIFICATION_CONFIGURED
    return _DEMO_PUBKEY_B64, VERIFICATION_DEMO


def _invalid(reason: str, *, verification: str) -> LicenseState:
    return LicenseState(
        status=STATUS_INVALID,
        verification=verification,
        # 篡改/错钥匙一律失败关闭：既不授予席位，也不允许继续写。
        # 这比"静默退回社区模式"更能暴露分发事故（拿到错文件要看得见）。
        read_only=True,
        reason=reason,
    )


def verify_license(raw: str | None,
                   *, public_key_b64: str | None = None) -> LicenseState:
    """校验一段 License 原文。

    返回的 `status` 只会是 missing / invalid_signature / expired / valid 之一。
    未知 feature 记入 `ignored_features`，不影响有效性。
    """
    if public_key_b64 is None:
        public_key_b64, verification = _configured_public_key()
    else:
        verification = VERIFICATION_CONFIGURED

    if raw is None or not raw.strip():
        # 社区模式：不限制席位、不限制功能、可写。
        return LicenseState(status=STATUS_MISSING, verification=verification)

    try:
        envelope = _decode_envelope(raw)
        payload = envelope.get("payload")
        signature_b64 = envelope.get("signature")
        if not isinstance(payload, dict):
            raise _LicenseFormatError("payload_not_object")
        if not isinstance(signature_b64, str) or not signature_b64.strip():
            raise _LicenseFormatError("signature_missing")
        public_key = _load_public_key(public_key_b64)
        try:
            signature = base64.b64decode(signature_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise _LicenseFormatError("signature_not_base64") from exc
        if len(signature) != 64:
            raise _LicenseFormatError("signature_length_invalid")
        public_key.verify(signature, canonical_payload(payload))
    except InvalidSignature:
        logger.warning("License 签名校验失败：公钥不匹配或内容被篡改")
        return _invalid("bad_signature", verification=verification)
    except _LicenseFormatError as exc:
        logger.warning("License 格式不合法：%s", exc.reason)
        return _invalid(exc.reason, verification=verification)

    try:
        licensee = _as_text(payload.get("licensee"), "licensee")
        instance = _as_text(payload.get("instance"), "instance")
        version_raw = payload.get("version")
        version = None if version_raw is None else str(version_raw)[:50]
        expires_at = _parse_datetime(payload.get("expires_at"), "expires_at")
        issued_at = _parse_datetime(payload.get("issued_at"), "issued_at")

        seats_raw = payload.get("seats")
        if seats_raw is None:
            seats = None
        elif isinstance(seats_raw, bool) or not isinstance(seats_raw, int) \
                or seats_raw < 0:
            raise _LicenseFormatError("seats_invalid")
        else:
            seats = seats_raw

        features_raw = payload.get("features")
        if features_raw is None:
            features_list: list[str] = []
        elif isinstance(features_raw, list) and all(
                isinstance(item, str) for item in features_raw):
            features_list = [item.strip() for item in features_raw if item.strip()]
        else:
            raise _LicenseFormatError("features_invalid")
    except _LicenseFormatError as exc:
        # 签名通过但字段不合法：属于发行方/分发事故，同样失败关闭。
        logger.warning("License 字段不合法：%s", exc.reason)
        return _invalid(exc.reason, verification=verification)

    known = tuple(sorted({f for f in features_list if f in KNOWN_FEATURES}))
    ignored = tuple(sorted({f for f in features_list if f not in KNOWN_FEATURES}))
    if ignored:
        # 只记录名称（不是密钥），便于排查"版本落后于 License 能力"。
        logger.info("License 含未知 feature，已忽略：%s", ", ".join(ignored))

    now = datetime.now(timezone.utc)
    expired = expires_at is not None and expires_at <= now
    if expired:
        logger.warning("License 已于 %s 过期，进入只读模式", expires_at.isoformat())

    return LicenseState(
        status=STATUS_EXPIRED if expired else STATUS_VALID,
        seats=seats,
        features=known,
        ignored_features=ignored,
        expires_at=expires_at,
        issued_at=issued_at,
        licensee=licensee,
        instance=instance,
        version=version,
        verification=verification,
        read_only=expired,
        reason="expired" if expired else "",
    )


# ---------------------------------------------------------------------------
# 缓存：按 License 原文指纹 + 公钥复用校验结果
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: tuple[tuple, LicenseState] | None = None


def _license_file_path() -> Path:
    raw = (os.environ.get(LICENSE_FILE_ENV) or "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_LICENSE_FILE
    if not path.is_absolute():
        path = (API_ROOT / path).resolve()
    return path


def _source_fingerprint() -> tuple[tuple, str | None, str]:
    """返回 (缓存指纹, 内联原文或 None, 来源说明)。只做 env 读取 + 文件 stat。"""
    inline = (os.environ.get(LICENSE_ENV) or "").strip()
    if inline:
        digest = hashlib.sha256(inline.encode("utf-8")).hexdigest()
        return ("env", digest), inline, "env"

    path = _license_file_path()
    try:
        stat = path.stat()
    except OSError:
        return ("missing", str(path)), None, "none"
    if not path.is_file():
        return ("missing", str(path)), None, "none"
    return ("file", str(path), stat.st_mtime_ns, stat.st_size), None, f"file:{path}"


def get_state() -> LicenseState:
    """当前 License 状态（带缓存）。未配置 = 社区模式。"""
    global _cache
    public_key_b64, verification = _configured_public_key()
    fingerprint, inline, source = _source_fingerprint()
    key = (fingerprint, public_key_b64)

    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]

        raw = inline
        if raw is None and source.startswith("file:"):
            path = Path(source.split(":", 1)[1])
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                # 文件存在但读不动：按未配置处理会让"License 失效"悄无声息，
                # 所以只记录并交给 verify_license(None) 之前先标记为无效。
                logger.warning("License 文件不可读（%s）：%s", path, exc)
                state = replace(_invalid("file_unreadable",
                                         verification=verification),
                                source=source)
                _cache = (key, state)
                return state

        state = verify_license(raw, public_key_b64=public_key_b64)
        state = replace(state, verification=verification, source=source)
        _cache = (key, state)
        logger.info("License 状态：status=%s source=%s verification=%s",
                    state.status, state.source, state.verification)
        return state


def reset_cache() -> None:
    """清空缓存（测试与"手工替换 License 后立即生效"场景用）。"""
    global _cache
    with _cache_lock:
        _cache = None


# ---------------------------------------------------------------------------
# 便捷判定（业务代码只用这三个）
# ---------------------------------------------------------------------------

def seat_limit() -> int | None:
    """席位上上限；None = 社区模式/未配置，表示不限制。"""
    return get_state().seats


def has_feature(name: str) -> bool:
    """当前 License 是否携带某能力；社区模式恒为 False。"""
    return (name or "").strip() in get_state().features


def is_read_only() -> bool:
    """是否只读（过期或签名不可信）。社区模式恒为 False。"""
    return get_state().read_only
