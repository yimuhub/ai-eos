"""模型供应商管理：增删改查、密钥写入、连通测试、拉取与导入模型。

与旧 `models_api` 的分工：

* 供应商（本文件）保存「连得上谁」——地址、密钥变量名、供应商级配置；
* 模型（`/models`）保存「用哪个模型、什么类型、怎么调用」。

安全边界沿用模型配置那一套：供应商可见性按公司隔离；写/删要求 MANAGE；
密钥只写本机 .env（仅超管、白名单变量名、600 权限、只回显掩码）；
base_url 过出站闸门，拉取失败不回显上游响应体。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.models_api import (_my_company_ids, _reject_sensitive_env,
                                       _validate_model_base_url)
from app.core.env_store import (is_allowed_model_env, mask_key,
                                persist_env_key, provider_env_name)
from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.core.permissions import Capability, ensure_capability
from app.db.session import get_db
from app.model_discovery import (default_config_for, discover_models,
                                 provider_default_config)
from app.models.agent import Model, ModelProviderType, ModelType
from app.models.model_provider import ModelProvider
from app.models.user import User
from app.schemas.responses_ops import (ModelProviderImportOut,
                                       ModelProviderItemOut,
                                       ProviderDiscoverOut)

router = APIRouter(prefix="/model-providers", tags=["model-providers"])

_SAFE_NAME = re.compile(r"[\s\"'`\\]")


async def _provider_key(db: AsyncSession, provider: ModelProvider) -> str:
    """按模型网关同一套解析链取密钥：公司密钥库（BYOK）→ 环境变量。

    连通测试必须用"运行时真正会用的那把钥匙"，否则客户把 Key 存进密钥库后，
    测试会报"缺密钥"，而任务其实能跑通。
    """
    from app.services import api_key_resolver

    resolved = await api_key_resolver.resolve(
        db, company_id=provider.company_id,
        api_key_env=provider.api_key_env,
        provider_type=provider.provider_type)
    return resolved.value


async def _vault_names_for(db: AsyncSession,
                           providers: list[ModelProvider]) -> frozenset[str]:
    """这些供应商所属公司的密钥库里有那些名字（不解密）。"""
    from app.services import api_key_resolver

    return await api_key_resolver.vault_names(
        db, [p.company_id for p in providers])


def _key_present(provider: ModelProvider,
                 vault_names: frozenset[str] = frozenset()) -> bool:
    """"已配置"：环境变量/密钥库任一有值即为 True。"""
    from app.services import api_key_resolver

    for name in api_key_resolver.candidate_names(
            provider.api_key_env, provider.provider_type):
        if name in vault_names:
            return True
    return bool(api_key_resolver.env_key(
        provider.api_key_env, provider.provider_type))


async def _visible_providers(db: AsyncSession, user: User) -> list[ModelProvider]:
    mine = await _my_company_ids(db, user.id)
    rows = await db.execute(select(ModelProvider).where(
        or_(ModelProvider.company_id.is_(None),
            ModelProvider.company_id.in_(mine))))
    return list(rows.scalars())


async def _get_provider(db: AsyncSession, user: User, pid: str,
                        capability: Capability) -> ModelProvider:
    provider = await db.get(ModelProvider, pid)
    if provider is None:
        raise HTTPException(404, "供应商不存在")
    if provider.company_id is None:
        # 无主供应商是升级遗留：可读、可拉取，但不能写（先认领）。
        if capability is Capability.VIEW:
            return provider
        raise HTTPException(
            409, "该供应商未归属公司（升级遗留配置），请先认领后再修改。")
    await ensure_capability(db, user.id, provider.company_id, capability)
    return provider


def _provider_out(provider: ModelProvider, *, model_count: int = 0,
                  enabled_count: int = 0,
                  vault_names: frozenset[str] = frozenset()) -> dict:
    return {
        "id": provider.id,
        "name": provider.name,
        "provider_type": provider.provider_type.value,
        "base_url": provider.base_url,
        "api_key_env": provider.api_key_env,
        "api_key_present": _key_present(provider, vault_names),
        "enabled": provider.enabled is not False,
        "config": provider.config or {},
        "company_id": provider.company_id,
        "unowned": provider.company_id is None,
        "model_count": model_count,
        "enabled_model_count": enabled_count,
        "last_check_ok": provider.last_check_ok,
        "last_check_at": (provider.last_check_at.isoformat()
                          if provider.last_check_at else None),
        "last_check_error": provider.last_check_error,
    }


def _record_check(provider: ModelProvider, ok: bool, error: str | None) -> None:
    provider.last_check_ok = ok
    provider.last_check_at = datetime.now(timezone.utc)
    provider.last_check_error = (str(error)[:500] if error else None)


class ProviderIn(BaseModel):
    name: str
    provider_type: str = "openai_compat"
    base_url: str | None = None
    api_key_env: str | None = None
    company_id: str
    enabled: bool = True
    config: dict | None = None


class ProviderPatch(BaseModel):
    name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    enabled: bool | None = None
    config: dict | None = None
    # 仅用于「认领」升级遗留的无主供应商；正常修改不必传
    company_id: str | None = None


class ProviderKeyIn(BaseModel):
    api_key: str


class ProviderPullIn(BaseModel):
    # 允许"先试再存"：密钥只用于本次拉取，不落库、不写 .env。
    api_key: str | None = None


class ImportItem(BaseModel):
    model_id: str
    name: str | None = None
    model_type: str | None = None
    multimodal: bool | None = None
    capabilities: list | None = None
    config: dict | None = None
    upstream: dict | None = None
    enabled: bool | None = None
    overwrite_config: bool = False


class ImportIn(BaseModel):
    models: list[ImportItem]


def _parse_provider_type(value: str) -> ModelProviderType:
    try:
        return ModelProviderType(value)
    except ValueError:
        raise HTTPException(
            400, f"provider_type 只能是 {[p.value for p in ModelProviderType]}")


def _parse_model_type(value: str | None) -> str:
    if not value:
        return ModelType.TEXT.value
    try:
        return ModelType(str(value)).value
    except ValueError:
        raise HTTPException(400, f"model_type 只能是 {[t.value for t in ModelType]}")


@router.get("", response_model=list[ModelProviderItemOut])
async def list_model_providers(db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """列出当前用户可见的供应商（自己公司的 + 升级遗留的无主配置）。"""
    providers = await _visible_providers(db, user)
    counts: dict[str, list[int]] = {}
    if providers:
        rows = await db.execute(select(Model).where(
            Model.provider_id.in_([p.id for p in providers])))
        for m in rows.scalars():
            slot = counts.setdefault(m.provider_id or "", [0, 0])
            slot[0] += 1
            if m.enabled is not False:
                slot[1] += 1
    vault = await _vault_names_for(db, providers)
    return [_provider_out(p, model_count=counts.get(p.id, [0, 0])[0],
                          enabled_count=counts.get(p.id, [0, 0])[1],
                          vault_names=vault)
            for p in sorted(providers, key=lambda x: (x.company_id is not None,
                                                      x.name))]


@router.get("/{pid}", response_model=ModelProviderItemOut)
async def get_model_provider(pid: str, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    provider = await _get_provider(db, user, pid, Capability.VIEW)
    rows = await db.execute(select(Model).where(Model.provider_id == pid))
    models = list(rows.scalars())
    return _provider_out(
        provider, model_count=len(models),
        enabled_count=sum(1 for m in models if m.enabled is not False),
        vault_names=await _vault_names_for(db, [provider]))


@router.post("", status_code=201, response_model=ModelProviderItemOut)
async def create_model_provider(data: ProviderIn,
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    """添加供应商。可无限添加，每个供应商有自己的地址与密钥变量。"""
    await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
    provider_type = _parse_provider_type(data.provider_type)
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "供应商名称不能为空。")
    if len(name) > 120:
        raise HTTPException(400, "供应商名称不能超过 120 个字符。")
    base_url = (data.base_url or "").strip() or None
    if provider_type is ModelProviderType.OPENAI_COMPAT and not base_url:
        raise HTTPException(400, "OpenAI 兼容供应商必须填写 Base URL。")
    if base_url:
        try:
            await validate_outbound_url_async(base_url, allow_private=True)
        except OutboundURLRejected as e:
            raise HTTPException(400, f"base_url 不安全：{e}")

    duplicate = (await db.execute(select(ModelProvider.id).where(
        ModelProvider.company_id == data.company_id,
        ModelProvider.name == name))).first()
    if duplicate:
        raise HTTPException(409, f"当前公司已有名为「{name}」的供应商。")

    env_name = (data.api_key_env or "").strip() or None
    if env_name:
        if not is_allowed_model_env(env_name):
            raise HTTPException(
                400, "api_key_env 只能是默认变量或 AIEOS_PROVIDER_*_API_KEY 形式。")
        _reject_sensitive_env(env_name)

    provider_id = str(uuid.uuid4())
    if not env_name:
        # 带上 id 片段：两个「OpenAI 兼容」供应商不能用同一个变量名，
        # 否则后写入的 Key 会静默覆盖前一个。
        env_name = provider_env_name(provider_type.value,
                                     f"{name}_{provider_id[:8]}")
    config = dict(provider_default_config(provider_type))
    if isinstance(data.config, dict):
        config.update(data.config)

    provider = ModelProvider(
        id=provider_id, company_id=data.company_id, name=name,
        provider_type=provider_type, base_url=base_url,
        api_key_env=env_name, enabled=bool(data.enabled), config=config)
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return _provider_out(
        provider, vault_names=await _vault_names_for(db, [provider]))


@router.patch("/{pid}", response_model=ModelProviderItemOut)
async def update_model_provider(pid: str, data: ProviderPatch,
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    provider = await _get_provider(db, user, pid, Capability.VIEW)
    children = (await db.execute(select(Model).where(
        Model.provider_id == provider.id))).scalars().all()

    if provider.company_id is None:
        # 无主供应商只能被认领：认领后它和下面的无主模型一起归入公司。
        if not data.company_id:
            raise HTTPException(
                409, "该供应商未归属公司（升级遗留配置），请先认领后再修改。")
        await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
        provider.company_id = data.company_id
        for m in children:
            if m.company_id is None:
                m.company_id = data.company_id
        await db.commit()
        await db.refresh(provider)
        return _provider_out(
            provider, model_count=len(children),
            enabled_count=sum(1 for m in children
                              if m.enabled is not False),
            vault_names=await _vault_names_for(db, [provider]))

    await ensure_capability(db, user.id, provider.company_id, Capability.MANAGE)

    if data.company_id and data.company_id != provider.company_id:
        # 转让归属要求两边都有权限，否则等于"把别人的供应商搬到我家"
        await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
        provider.company_id = data.company_id
        # 供应商和它下面的模型是一体的：只搬供应商会让模型留在原公司，
        # 变成"公司的供应商下挂着别家的模型"。
        for m in children:
            m.company_id = data.company_id

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "供应商名称不能为空。")
        provider.name = name
    if data.provider_type is not None:
        provider.provider_type = _parse_provider_type(data.provider_type)
    if data.base_url is not None:
        base_url = data.base_url.strip() or None
        if provider.provider_type is ModelProviderType.OPENAI_COMPAT and not base_url:
            raise HTTPException(400, "OpenAI 兼容供应商必须填写 Base URL。")
        if base_url:
            try:
                await validate_outbound_url_async(base_url, allow_private=True)
            except OutboundURLRejected as e:
                raise HTTPException(400, f"base_url 不安全：{e}")
        provider.base_url = base_url
    if data.api_key_env is not None:
        env_name = data.api_key_env.strip()
        if not is_allowed_model_env(env_name):
            raise HTTPException(
                400, "api_key_env 只能是默认变量或 AIEOS_PROVIDER_*_API_KEY 形式。")
        _reject_sensitive_env(env_name)
        provider.api_key_env = env_name
    if data.enabled is not None:
        provider.enabled = data.enabled
    if data.config is not None:
        merged = dict(provider.config or {})
        merged.update(data.config)
        provider.config = merged

    # 供应商是模型运行时字段的事实源：改了地址/类型/密钥变量后，
    # 其下模型必须同步，否则界面显示新地址、实际请求还打旧地址。
    for m in children:
        m.provider_type = provider.provider_type
        m.base_url = provider.base_url
        m.api_key_env = provider.api_key_env
    await db.commit()
    await db.refresh(provider)
    return _provider_out(
        provider, model_count=len(children),
        enabled_count=sum(1 for m in children if m.enabled is not False),
        vault_names=await _vault_names_for(db, [provider]))


@router.delete("/{pid}", status_code=204)
async def delete_model_provider(pid: str, db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    provider = await _get_provider(db, user, pid, Capability.MANAGE)
    count = (await db.execute(select(Model.id).where(
        Model.provider_id == pid))).first()
    if count is not None:
        raise HTTPException(
            409, "该供应商下还有模型，请先删除其模型（或把它们转到别的供应商）"
                 "再删除供应商，避免员工绑定悬空。")
    await db.delete(provider)
    await db.commit()


@router.post("/{pid}/key")
async def save_provider_key(pid: str, data: ProviderKeyIn,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """把该供应商的密钥写进本机 .env 并立即生效（仅超管）。"""
    provider = await _get_provider(db, user, pid, Capability.MANAGE)
    if not user.is_superadmin:
        raise HTTPException(403, "只有系统管理员可以写入服务端密钥。")
    key = (data.api_key or "").strip()
    if not 8 <= len(key) <= 512:
        raise HTTPException(400, "密钥长度应在 8–512 个字符之间。")
    if _SAFE_NAME.search(key):
        raise HTTPException(400, "密钥里不能包含空白字符或引号，请检查是否多复制了内容。")
    env_name = provider.api_key_env or provider_env_name(
        provider.provider_type.value, f"{provider.name}_{provider.id[:8]}")
    if not is_allowed_model_env(env_name):
        raise HTTPException(400, "该供应商的密钥变量名不合法。")
    try:
        persist_env_key(env_name, key)
    except OSError as e:
        raise HTTPException(
            500, f"写入 .env 失败：{e.strerror or e}。请确认后端目录有写权限。")

    provider.api_key_env = env_name
    # 运行时只读 models.api_key_env；同步子模型，避免"供应商 key 写好了、
    # 模型仍指向默认变量"的静默 401。
    for m in (await db.execute(select(Model).where(
            Model.provider_id == provider.id))).scalars():
        m.api_key_env = env_name
    await db.commit()
    return {"saved": True, "env_name": env_name, "masked": mask_key(key),
            "applied": True}


@router.post("/{pid}/test", response_model=ProviderDiscoverOut)
async def test_model_provider(pid: str, db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """连通测试：只拉模型列表，不发推理请求，不消耗 token。"""
    provider = await _get_provider(db, user, pid, Capability.VIEW)
    try:
        result = await discover_models(
            provider.provider_type, provider.base_url or "",
            await _provider_key(db, provider), provider.config or {})
    except OutboundURLRejected as e:
        raise HTTPException(400, f"base_url 不安全：{e}")
    _record_check(provider, result.ok, result.error)
    await db.commit()
    return {
        "status": "ok" if result.ok else "error",
        "models": [m["id"] for m in result.models],
        "items": result.models,
        "base_url": provider.base_url,
        "error": result.error,
        "model_count": len(result.models),
    }


@router.post("/{pid}/pull", response_model=ProviderDiscoverOut)
async def pull_model_provider(pid: str, data: ProviderPullIn | None = None,
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """按供应商已存配置拉取模型；也允许临时传入 key（不落库）先试。"""
    provider = await _get_provider(db, user, pid, Capability.VIEW)
    key = (data.api_key.strip() if data and data.api_key else "") or \
        await _provider_key(db, provider)
    try:
        result = await discover_models(
            provider.provider_type, provider.base_url or "", key,
            provider.config or {})
    except OutboundURLRejected as e:
        raise HTTPException(400, f"base_url 不安全：{e}")
    # 临时 key 不改变持久化的连通状态；用已存 key 的结果才落库。
    if not (data and data.api_key):
        _record_check(provider, result.ok, result.error)
        await db.commit()
    return {
        "status": "ok" if result.ok else "error",
        "models": [m["id"] for m in result.models],
        "items": result.models,
        "base_url": provider.base_url,
        "error": result.error,
        "model_count": len(result.models),
    }


@router.post("/{pid}/import", response_model=ModelProviderImportOut)
async def import_provider_models(pid: str, data: ImportIn,
                                 db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """把勾选的模型导入供应商，自动写入类型与按类型的默认配置。

    幂等：同一供应商下同一 model_id 只保留一行；再次导入会刷新上游字段，
    但不会覆盖用户手工改过的 config（除非显式 overwrite_config=true）。
    """
    provider = await _get_provider(db, user, pid, Capability.MANAGE)
    if provider.company_id is None:
        raise HTTPException(409, "该供应商未归属公司，不能导入模型。")
    if not data.models:
        raise HTTPException(400, "请至少选择一个模型。")

    existing = (await db.execute(select(Model).where(
        Model.provider_id == provider.id))).scalars().all()
    by_model_id = {m.model_id: m for m in existing}
    # 升级前的直连行同公司、同端点、同模型 id 时直接收编，避免重复两行。
    legacy = (await db.execute(select(Model).where(
        Model.company_id == provider.company_id,
        Model.provider_id.is_(None),
        Model.base_url == provider.base_url))).scalars().all()
    by_legacy_id = {m.model_id: m for m in legacy}

    created: list[str] = []
    updated: list[str] = []
    adopted: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for item in data.models:
        mid = (item.model_id or "").strip()
        if not mid:
            raise HTTPException(400, "model_id 不能为空。")
        if mid in seen:
            skipped.append(mid)
            continue
        seen.add(mid)
        mtype = _parse_model_type(item.model_type)
        upstream = item.upstream if isinstance(item.upstream, dict) else {}
        auto_config = default_config_for(
            mtype, {"context_window": upstream.get("context_window"),
                    "max_output_tokens": upstream.get("max_output_tokens")}, mid)
        target = by_model_id.get(mid)
        state = "created"
        if target is None:
            target = by_legacy_id.get(mid)
            if target is not None:
                state = "adopted"
                target.provider_id = provider.id
        if target is None:
            target = Model(
                company_id=provider.company_id, provider_id=provider.id,
                provider_type=provider.provider_type,
                base_url=provider.base_url, api_key_env=provider.api_key_env,
                model_id=mid)
            db.add(target)
        else:
            target.company_id = provider.company_id or target.company_id
            target.provider_type = provider.provider_type
            target.base_url = provider.base_url
            target.api_key_env = provider.api_key_env

        caps = (item.capabilities if item.capabilities is not None
                else target.capabilities or [])
        target.name = (item.name or target.name or mid)[:200]
        target.model_type = mtype
        if item.multimodal is not None:
            target.multimodal = bool(item.multimodal)
        else:
            target.multimodal = (mtype in {ModelType.IMAGE.value,
                                           ModelType.VIDEO.value}
                                 or "vision" in caps)
        target.capabilities = caps
        target.upstream_meta = upstream
        target.last_synced_at = now
        if item.enabled is not None:
            target.enabled = item.enabled
        elif target.enabled is None:
            target.enabled = True

        keep_manual = (target.config_source == "manual"
                       and not item.overwrite_config
                       and isinstance(target.config, dict))
        if keep_manual:
            if isinstance(item.config, dict):
                merged = dict(target.config)
                merged.update(item.config)
                target.config = merged
        else:
            merged = dict(auto_config)
            if isinstance(item.config, dict):
                merged.update(item.config)
            target.config = merged
            target.config_source = "manual" if item.config else "auto"

        if state == "created":
            created.append(mid)
        elif state == "adopted":
            adopted.append(mid)
        else:
            updated.append(mid)

    await db.commit()
    return {"imported": created, "updated": updated, "adopted": adopted,
            "skipped": skipped,
            "imported_count": len(created) + len(adopted) + len(updated)}
