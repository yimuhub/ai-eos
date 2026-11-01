"""BYOK 密钥保险库（E8）。

## 边界

* 数据库里只存 Fernet 密文；明文只在请求内存里出现，**绝不写日志、
  绝不回显、绝不落库**。
* 加密密钥不单独配置：从 `settings.jwt_secret` 经 HKDF-SHA256 派生
  （固定 salt / info）。这样便携部署无需多管一份密钥，同时不同用途的
  Fernet key 与 JWT 签名密钥在密码学上相互独立。
* `jwt_secret` 由配置层保证非空（首次启动会自动生成并落盘），
  因此正常部署下 `vault_available()` 恒为 True。
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

# 版本化 salt / info：将来轮换派生参数时改版本串，旧密文自然失效
# （密文本身带 Fernet 版本与 HMAC，派生出错会解密失败而不是解出乱码）。
_SALT = b"aieos-byok-v1"
_INFO = b"company-secret"
_KEY_LENGTH = 32


def _fernet() -> Fernet:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise RuntimeError("jwt_secret 未配置，密钥库不可用")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH,
        salt=_SALT,
        info=_INFO,
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def vault_available() -> bool:
    """密钥库是否可用（配置层正常时恒为 True）。"""
    try:
        _fernet()
    except Exception:  # noqa: BLE001 — 可用性探针不应抛出
        return False
    return True


def encrypt(value: str) -> str:
    """明文 → Fernet 密文（ASCII 字符串，可直接入库）。"""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Fernet 密文 → 明文。密文被篡改 / 密钥不匹配时抛 InvalidToken。"""
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
