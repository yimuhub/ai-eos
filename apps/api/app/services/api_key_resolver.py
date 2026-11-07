"""模型 API Key 解析：运行时、自检与供应商连通测试共用的唯一事实来源。

## 优先级（高 → 低）

1. 公司密钥库（BYOK，`company_secrets`）中与模型 `api_key_env` 同名的条目
2. 公司密钥库中与 provider 默认变量名同名的条目（如 `OPENAI_COMPAT_API_KEY`）
3. 进程环境变量 / `.env`（`settings.resolved_api_key`）

**客户密钥优先于平台密钥**是 BYOK 的语义：租户自带 Key 就该用自己的额度与
账单。若环境变量永远压过客户密钥，托管多租户下 BYOK 形同虚设——客户以为
用的是自己的 Key，实际烧的是平台的。

## 边界

* 明文只在内存里出现：`vault_key()` 解出后直接交给调用器，**不写日志、
  不进响应、不落库**。日志只记来源与名称。
* 解密失败（换过 `jwt_secret`）不阻断调用：落 warning 后继续走环境变量，
  否则一次密钥轮换会让整个租户的模型调用全部挂掉。
* 只读：本模块不创建、不修改 `company_secrets` 行。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_sensitive_env_name, settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# provider → 默认密钥变量名。与 `env_store.PROVIDER_ENV_VARS` 同源语义，
# 但这里不 import 路由层模块（服务层不该依赖 API 层）。
DEFAULT_ENV_NAMES: dict[str, str] = {
    "openai_compat": "OPENAI_COMPAT_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

SOURCE_VAULT = "company_vault"
SOURCE_ENV = "env"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class ResolvedKey:
    """一次密钥解析的结果。`value` 为空表示确实没有可用密钥。"""

    value: str
    source: str
    name: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.value)


def _provider_name(provider_type) -> str:
    return str(getattr(provider_type, "value", provider_type) or "")


def candidate_names(api_key_env: str | None,
                    provider_type) -> list[str]:
    """该模型会去密钥库/环境里找的变量名，按优先级排列。

    敏感变量名（JWT_SECRET 等）一律不参与：即使历史数据或直接改库让
    `api_key_env` 指向服务端密钥，运行时也取不到值。
    """
    names: list[str] = []
    env_name = (api_key_env or "").strip()
    if env_name and not is_sensitive_env_name(env_name):
        names.append(env_name)
    default = DEFAULT_ENV_NAMES.get(_provider_name(provider_type))
    if default and default not in names:
        names.append(default)
    return names


async def vault_names(db: AsyncSession,
                      company_ids: Iterable[str]) -> frozenset[str]:
    """这些公司密钥库里有哪些名字（只查名字，不解密、不取值）。

    用于「密钥是否已配置」类自检：知道存在即可，没有必要在列表接口里
    解密客户的全部密钥。
    """
    ids = [c for c in dict.fromkeys(company_ids) if c]
    if not ids:
        return frozenset()
    from app.models.company_secret import CompanySecret

    rows = await db.execute(
        select(CompanySecret.name).where(CompanySecret.company_id.in_(ids)))
    return frozenset(row[0] for row in rows.all())


async def vault_key(db: AsyncSession, company_id: str | None,
                    api_key_env: str | None,
                    provider_type) -> str | None:
    """取该公司密钥库里第一个可用的密钥明文；没有则返回 None。

    按 `candidate_names()` 的顺序逐个尝试，命中即返回。解密失败只记
    warning（不含密文/明文），继续尝试下一个名字。
    """
    if not company_id:
        return None
    names = candidate_names(api_key_env, provider_type)
    if not names:
        return None

    from app.models.company_secret import CompanySecret
    from app.services import secret_vault

    rows = (await db.execute(
        select(CompanySecret)
        .where(CompanySecret.company_id == company_id,
               CompanySecret.name.in_(names)))).scalars().all()
    by_name = {row.name: row for row in rows}
    for name in names:
        row = by_name.get(name)
        if row is None:
            continue
        try:
            value = secret_vault.decrypt(row.ciphertext)
        except Exception as exc:  # noqa: BLE001 — 轮换后解不开要降级而非阻断
            logger.warning(
                "公司密钥库条目 %s 解密失败（%s），本次改用环境变量密钥",
                name, type(exc).__name__)
            continue
        if value:
            return value
    return None


def env_key(api_key_env: str | None, provider_type) -> str:
    """平台侧密钥：环境变量 / `.env`（含 `<ENV>_FILE` 注入）。"""
    provider = _provider_name(provider_type)
    value = settings.resolved_api_key(api_key_env, provider)
    if value:
        return value
    names = candidate_names(api_key_env, provider_type)
    for name in names:
        injected = os.environ.get(f"{name}_FILE", "")
        if injected:
            return injected
    return ""


async def resolve(db: AsyncSession, *, company_id: str | None,
                  api_key_env: str | None,
                  provider_type) -> ResolvedKey:
    """完整解析链：公司密钥库 → 环境变量。`value` 为空代表没有密钥。"""
    value = await vault_key(db, company_id, api_key_env, provider_type)
    if value:
        # 名称不在这里回填：命中的可能是候选里的任意一个，不猜。
        return ResolvedKey(value, SOURCE_VAULT)
    value = env_key(api_key_env, provider_type)
    if value:
        return ResolvedKey(value, SOURCE_ENV, api_key_env)
    return ResolvedKey("", SOURCE_NONE)


async def resolve_for_companies(db: AsyncSession, company_ids: Iterable[str],
                                api_key_env: str | None,
                                provider_type) -> ResolvedKey:
    """无主资源（共享模型/供应商）的解析：先在调用方各公司的密钥库里找，
    都找不到再回落到环境变量。"""
    ids = [c for c in dict.fromkeys(company_ids) if c]
    for company_id in ids:
        value = await vault_key(db, company_id, api_key_env, provider_type)
        if value:
            return ResolvedKey(value, SOURCE_VAULT)
    value = env_key(api_key_env, provider_type)
    if value:
        return ResolvedKey(value, SOURCE_ENV, api_key_env)
    return ResolvedKey("", SOURCE_NONE)
