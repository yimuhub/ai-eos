"""行业包 API（V6 M4）：内置目录、发布进市场、公司功能开关。

发布（superadmin）：把内置包内容注册成 MarketplacePackage（全局目录），
公司安装走统一市场路径（MANAGE + 权限确认 + artifacts 可回滚）。
功能开关：per-company 键值，行业包安装时自动点亮 industry_pack:<id>。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.feature_flags import CompanyFeatureFlag
from app.models.p3_governance import MarketplacePackage
from app.models.user import User
from app.schemas.responses_v61 import (FeatureListOut, FeatureSetOut,
                                        IndustryPacksOut, IndustryPublishOut)
from app.services.industry_pack_service import (get_builtin, list_builtins,
                                                validate_content)

router = APIRouter(prefix="/industry-packs", tags=["industry-packs"])

# 行业包内容里允许进 manifest 的键（服务层种植时也按这份白名单取）
# V6.1 新增 compliance_items（合规台账种子）与 integration_targets（连接器蓝图）
_PACK_KEYS = ("id", "name", "version", "description", "terms", "custom_fields",
              "workflow_templates", "metric_defs", "agent_roster",
              "kb_seed_topics", "compliance_items", "integration_targets")


@router.get("", response_model=IndustryPacksOut)
async def list_packs(db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """内置行业包目录 + 各包的市场发布状态。"""
    packs = list_builtins()
    published = set((await db.execute(
        select(MarketplacePackage.key).where(
            MarketplacePackage.package_type == "industry_pack",
            MarketplacePackage.published.is_(True)))).scalars().all())
    return {"packs": [{
        "id": p["id"], "name": p["name"], "version": p["version"],
        "description": p["description"],
        "custom_fields": len(p.get("custom_fields") or []),
        "workflow_templates": len(p.get("workflow_templates") or []),
        "metric_defs": len(p.get("metric_defs") or []),
        "agent_roster": len(p.get("agent_roster") or []),
        "published": p["id"] in published,
    } for p in packs]}


@router.post("/publish", status_code=201,
             response_model=IndustryPublishOut)
async def publish_pack(body: dict,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """把内置行业包发布进市场目录（全局操作，超管专属）。"""
    if not user.is_superadmin:
        raise HTTPException(403, "发布行业包需要平台超管")
    pack_id = str(body.get("pack_id") or "")
    pack = get_builtin(pack_id)
    if pack is None:
        raise HTTPException(404, f"内置行业包 {pack_id!r} 不存在")
    try:
        validate_content(pack)
    except ValueError as exc:
        raise HTTPException(400, f"行业包内容非法：{exc}") from exc

    manifest = {k: pack.get(k) for k in _PACK_KEYS}
    manifest.update({"category": "industry_pack", "runtime": "declarative"})
    existing = (await db.execute(
        select(MarketplacePackage).where(
            MarketplacePackage.package_type == "industry_pack",
            MarketplacePackage.key == pack["id"]))).scalars().first()
    if existing:
        # 内容演进：覆盖 manifest，版本号同步
        existing.manifest = manifest
        existing.latest_version = str(pack.get("version"))
        existing.name = str(pack.get("name"))
        existing.description = pack.get("description")
        existing.permissions_required = ["data:canonical:rw"]
        await db.commit()
        return {"ok": True, "package_id": existing.id, "updated": True,
                "key": existing.key, "version": existing.latest_version}
    row = MarketplacePackage(
        id=str(uuid.uuid4()), package_type="industry_pack", key=pack["id"],
        name=str(pack.get("name")), description=pack.get("description"),
        author="AI-EOS 内置", latest_version=str(pack.get("version")),
        permissions_required=["data:canonical:rw"], dependencies=[],
        manifest=manifest, published=True)
    db.add(row)
    await db.commit()
    return {"ok": True, "package_id": row.id, "updated": False,
            "key": row.key, "version": row.latest_version}


# ---------------------------------------------------------------- 功能开关

features_router = APIRouter(prefix="/company-features",
                            tags=["company-features"])


class FeatureIn(BaseModel):
    key: str
    enabled: bool = True
    value: dict | None = None


@features_router.get("", response_model=FeatureListOut)
async def list_features(company_id: str = Query(...),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = (await db.execute(
        select(CompanyFeatureFlag).where(
            CompanyFeatureFlag.company_id == company_id)
        .order_by(CompanyFeatureFlag.key))).scalars().all()
    return {"features": [{
        "key": r.key, "enabled": r.enabled, "value": r.value,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}


@features_router.put("", response_model=FeatureSetOut)
async def set_feature(body: FeatureIn, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    from datetime import datetime, timezone

    if len(body.key) > 120 or not body.key.strip():
        raise HTTPException(400, "key 非法")
    row = (await db.execute(
        select(CompanyFeatureFlag).where(
            CompanyFeatureFlag.company_id == company_id,
            CompanyFeatureFlag.key == body.key))).scalars().first()
    if row is None:
        row = CompanyFeatureFlag(
            id=str(uuid.uuid4()), company_id=company_id, key=body.key,
            value=body.value or {}, enabled=body.enabled, updated_by=user.id,
            enabled_at=datetime.now(timezone.utc) if body.enabled else None)
        db.add(row)
    else:
        row.enabled = body.enabled
        if body.value is not None:
            row.value = body.value
        row.updated_by = user.id
        row.enabled_at = (datetime.now(timezone.utc)
                          if body.enabled else None)
    await db.commit()
    return {"ok": True, "key": body.key, "enabled": body.enabled}
