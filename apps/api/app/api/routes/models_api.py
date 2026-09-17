"""Model registry CRUD (Provider configs)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.model_gateway import PROVIDERS
from app.models.agent import Model, ModelProviderType

router = APIRouter(prefix="/models", tags=["models"])


class ModelIn(BaseModel):
    name: str
    provider_type: str  # openai_compat|anthropic|gemini
    base_url: str | None = None
    api_key_env: str | None = None
    model_id: str
    config: dict | None = {}


@router.get("")
async def list_models(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = await db.execute(select(Model))
    return [
        {"id": m.id, "name": m.name, "provider_type": m.provider_type.value,
         "base_url": m.base_url, "model_id": m.model_id, "config": m.config}
        for m in q.scalars()
    ]


@router.post("", status_code=201)
async def create_model(data: ModelIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    try:
        pt = ModelProviderType(data.provider_type)
    except ValueError:
        raise HTTPException(400, f"provider_type must be one of {list(PROVIDERS)}")
    payload = data.model_dump()
    payload.pop("provider_type", None)
    m = Model(**payload, provider_type=pt)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id, "name": m.name, "provider_type": m.provider_type.value}


@router.patch("/{mid}")
async def update_model(mid: str, data: dict, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
    allowed = {"name", "base_url", "api_key_env", "model_id", "config"}
    if "provider_type" in data:
        m.provider_type = ModelProviderType(data["provider_type"])
    for k, v in data.items():
        if k in allowed:
            setattr(m, k, v)
    await db.commit()
    return {"id": m.id, "name": m.name}


@router.delete("/{mid}", status_code=204)
async def delete_model(mid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    m = await db.get(Model, mid)
    if not m:
        raise HTTPException(404, "Model not found")
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
