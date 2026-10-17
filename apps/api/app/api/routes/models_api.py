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
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import Capability, ensure_capability
from app.db.session import get_db
from app.model_gateway import PROVIDERS
from app.models.agent import Model, ModelProviderType
from app.models.company import Company
from app.models.user import User

router = APIRouter(prefix="/models", tags=["models"])


async def _my_company_ids(db: AsyncSession, user_id: str) -> list[str]:
    rows = await db.execute(select(Company.id).where(Company.owner_id == user_id))
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

# 各 provider 对应的默认环境变量名（与 .env.example 保持一致）。
# 这里只用于"密钥是否就位"的判断，永远不读取/返回密钥内容。
PROVIDER_ENV_VARS: dict[str, str] = {
    "openai_compat": "OPENAI_COMPAT_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


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


class ModelIn(BaseModel):
    name: str
    provider_type: str  # openai_compat|anthropic|gemini
    base_url: str | None = None
    api_key_env: str | None = None
    model_id: str
    config: dict | None = {}
    # 归属公司。不传则登记为"未归属/共享"（本地单人使用时够用）。
    company_id: str | None = None


@router.get("/diagnostics")
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
    rows = list((await db.execute(select(Model).where(
        or_(Model.company_id.is_(None),
            Model.company_id.in_(await _my_company_ids(db, user.id)))))).scalars())
    providers = []
    for ptype, env_name in PROVIDER_ENV_VARS.items():
        configured = [m for m in rows
                      if m.provider_type.value == ptype]
        ready_of_type = [m for m in configured if key_available(m)]
        providers.append({
            "provider_type": ptype,
            "default_env_var": env_name,
            # 该 provider 下"至少有一个模型能取到密钥"即为就绪。
            # 只看默认环境变量会误报：用户在模型配置里填了自定义变量名
            # （api_key_env）时，系统其实可用，界面却显示"缺密钥"。
            "key_present": bool(ready_of_type),
            "models_ready": len(ready_of_type),
            "model_count": len(configured),
        })

    # 是否存在"至少一个模型 + 该模型的密钥已就位"的组合
    ready_models = [m.id for m in rows if key_available(m)]

    missing_hint = ""
    if not rows:
        missing_hint = "还没有配置任何模型。请添加一个模型（推荐 OpenAI 兼容地址）。"
    elif not ready_models:
        names = "、".join(
            sorted({m.api_key_env or PROVIDER_ENV_VARS.get(m.provider_type.value, "")
                    for m in rows} - {""}))
        missing_hint = (f"模型已登记但缺少密钥。请在 apps/api/.env 中设置 "
                        f"{names} 后重启后端服务。")

    return {
        "model_count": len(rows),
        "usable_model_ids": ready_models,
        "ready": bool(ready_models),
        "providers": providers,
        "hint": missing_hint,
    }


@router.get("")
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
    return [
        {"id": m.id, "name": m.name, "provider_type": m.provider_type.value,
         "base_url": m.base_url, "model_id": m.model_id, "config": m.config,
         "company_id": m.company_id,
         # 未归属的配置要显式标出来，前端才能提示去认领，
         # 否则用户只会疑惑"为什么这条我删不掉"。
         "unowned": m.company_id is None}
        for m in q.scalars()
    ]


@router.post("", status_code=201)
async def create_model(data: ModelIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    try:
        pt = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {list(PROVIDERS)}")
    if data.company_id:
        # 只能登记到自己有管理权的公司名下
        await ensure_capability(db, user.id, data.company_id, Capability.MANAGE)
    payload = data.model_dump()
    payload.pop("provider_type", None)
    m = Model(**payload, provider_type=pt)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id, "name": m.name, "provider_type": m.provider_type.value,
            "company_id": m.company_id}


@router.patch("/{mid}")
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

    allowed = {"name", "base_url", "api_key_env", "model_id", "config"}
    if "provider_type" in data:
        m.provider_type = ModelProviderType(data["provider_type"])
    for k, v in data.items():
        if k in allowed:
            setattr(m, k, v)
    await db.commit()
    return {"id": m.id, "name": m.name, "company_id": m.company_id}


@router.delete("/{mid}", status_code=204)
async def delete_model(mid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    await _ensure_model_access(db, user, m, Capability.DESTRUCTIVE)
    await db.delete(m)
    await db.commit()


@router.post("/{mid}/test")
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
    provider = PROVIDERS[m.provider_type](
        base_url=m.base_url or "", api_key=_resolve_api_key(m), model_id=m.model_id)
    try:
        result = await provider.chat([{"role": "user", "content": "回复 OK 两个字母即可"}],
                                     config={"max_tokens": 10})
        return {"status": "ok", "reply": result["content"][:100], "raw_model": result.get("raw_model")}
    except Exception as e:
        return {"status": "error", "error": str(e)[:500]}


class FetchModelsIn(BaseModel):
    base_url: str
    api_key: str | None = None      # 直接传 key（不落库）
    api_key_env: str | None = None  # 或环境变量名


@router.post("/providers/list-models")
async def list_provider_models(data: FetchModelsIn,
                               user=Depends(get_current_user)):
    """从 OpenAI 兼容供应商拉取可用模型列表（GET /v1/models）。"""
    import httpx

    key = data.api_key
    if not key and data.api_key_env:
        import os
        key = os.environ.get(data.api_key_env, "")
    if not key:
        # 无 key 也尝试请求（部分本地服务如 ollama/llama.cpp 不需要）
        key = ""

    url = data.base_url.rstrip("/")
    if not url.endswith("/models"):
        url = f"{url}/models" if url.endswith("/v1") else f"{url}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {key}"}
                                    if key else {})
            if resp.status_code == 401:
                return {"status": "error", "error": "认证失败（401）：请检查 API Key"}
            resp.raise_for_status()
            body = resp.json()
            models = [m.get("id") for m in body.get("data", []) if m.get("id")]
            return {"status": "ok", "base_url": data.base_url, "models": sorted(models)}
    except httpx.ConnectError:
        return {"status": "error", "error": f"无法连接 {data.base_url}"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}


class BatchSelectIn(BaseModel):
    name_prefix: str          # 配置显示名前缀，如 "Hermes本地"
    provider_type: str = "openai_compat"
    base_url: str
    api_key_env: str | None = None
    model_ids: list[str]      # 用户勾选要启用的模型 id 列表


@router.post("/providers/batch-import")
async def batch_import_models(data: BatchSelectIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """把勾选的模型批量注册为可用 Model 配置。"""
    try:
        pt = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {list(PROVIDERS)}")
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


@router.get("/convert-config")
async def get_convert_config(user=Depends(get_current_user)):
    from app.core.config import settings
    return {"convert_model_id": getattr(settings, "doc_convert_model_id", "") or ""}


@router.post("/convert-config")
async def set_convert_config(data: dict, user=Depends(get_current_user)):
    """设置专用文档转换大模型（低成本模型），用于上传资料→MD 转换。"""
    from app.core.config import settings
    settings.doc_convert_model_id = data.get("convert_model_id") or ""
    return {"ok": True, "convert_model_id": settings.doc_convert_model_id}
