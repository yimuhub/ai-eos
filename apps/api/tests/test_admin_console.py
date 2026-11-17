"""超级管理后台（V6.1.1）：门禁、总览、行业解锁/收回。

钉住的契约：
1. 门禁双因子：非超管 403；超管但密钥错 403；都对才放行；
2. 行业解锁 = 发布→自动安装进用户公司（产物落库）→ flag 点亮 → 行业类别写入；
3. 收回（uninstall=True）= flag 删除 + 产物按 artifacts 回收；
4. 总览只对超管开放。
"""
from __future__ import annotations

import uuid

from app.core.config import settings


def _key_header():
    return {"X-Super-Key": settings.super_key}


async def _make_superadmin(make_user) -> dict:
    user = await make_user()
    from app.db.session import AsyncSessionLocal
    from app.models.user import User as UserRow
    async with AsyncSessionLocal() as db:
        row = await db.get(UserRow, user["id"])
        row.is_superadmin = True
        await db.commit()
    return user


async def test_admin_gate_requires_both_factors(client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    # 非超管 + 对的密钥 → 403
    r = await client.post("/ops/admin/unlock",
                          headers={**tenants.alice["headers"], **_key_header()},
                          json={})
    assert r.status_code == 403
    # 超管 + 错密钥 → 403
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"], "X-Super-Key": "wrong"},
                          json={})
    assert r.status_code == 403
    # 超管 + 对密钥 → 200
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"], **_key_header()}, json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 缺密钥头 → 403
    r = await client.get("/ops/admin/overview", headers=admin["headers"])
    assert r.status_code == 403


async def test_overview_lists_users_and_companies(client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    h = {**admin["headers"], **_key_header()}
    r = await client.get("/ops/admin/overview", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    emails = {u["email"] for u in body["users"]}
    assert tenants.alice["email"] in emails and tenants.bob["email"] in emails
    co = next(c for c in body["companies"] if c["id"] == tenants.alice_co["id"])
    assert co["owner"]["username"] == tenants.alice["username"]


async def test_unlock_industry_installs_and_revoke_uninstalls(client, tenants,
                                                              make_user):
    admin = await _make_superadmin(make_user)
    h = {**admin["headers"], **_key_header()}
    co = tenants.alice_co["id"]

    # 解锁餐饮行业：自动安装进 alice 的公司
    r = await client.post(
        f"/ops/admin/companies/{co}/entitlements", headers=h,
        json={"industry": "restaurant", "packs_install": ["restaurant"]})
    assert r.status_code == 200, r.text
    assert any("解锁" in a for a in r.json()["actions"])

    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.agent import Agent
    from app.models.ops import ComplianceLedger
    from app.models.feature_flags import CompanyFeatureFlag
    async with AsyncSessionLocal() as db:
        agents = (await db.execute(
            select(Agent).where(Agent.company_id == co))).scalars().all()
        assert len(agents) >= 5, "餐饮包应预配 6 名员工（去重后 ≥5）"
        ledgers = (await db.execute(
            select(ComplianceLedger).where(
                ComplianceLedger.company_id == co))).scalars().all()
        assert len(ledgers) == 4  # 餐饮包合规台账
        flags = (await db.execute(
            select(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == co,
                CompanyFeatureFlag.key == "industry:restaurant"))).scalars().all()
        assert len(flags) == 1 and flags[0].enabled
    # 行业类别已写入
    r = await client.get(f"/ops/admin/companies/{co}", headers=h)
    assert r.json()["company"]["industry"] == "restaurant"

    # 收回（含卸载）：flag 删除 + 产物回收
    r = await client.post(
        f"/ops/admin/companies/{co}/entitlements", headers=h,
        json={"packs_remove": [{"key": "restaurant", "uninstall": True}]})
    assert r.status_code == 200
    async with AsyncSessionLocal() as db:
        agents = (await db.execute(
            select(Agent).where(Agent.company_id == co))).scalars().all()
        ledgers = (await db.execute(
            select(ComplianceLedger).where(
                ComplianceLedger.company_id == co))).scalars().all()
        flags = (await db.execute(
            select(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == co))).scalars().all()
    assert agents == [] and ledgers == [] and flags == []
