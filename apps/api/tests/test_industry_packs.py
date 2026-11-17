"""行业包（V6 M4）：发布、一键种植、幂等、回滚、功能开关。

钉住的契约：
1. 内置目录 = 磁盘内容事实源（10 包全部通过校验）；
2. 发布超管闸门；发布后走统一市场安装路径（MANAGE + 权限确认）；
3. 安装一次事务种植：扩展字段 / 预配员工（不绑模型）/ 草稿工作流（AGENT
   节点轮指员工）/ 待接线指标 / 知识文档 / 功能开关，全部记 artifacts；
4. 重复安装幂等（按公司+键判重，不翻倍）；
5. 回滚按产物清单回收。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.canonical import CustomFieldDef, MetricDef
from app.models.feature_flags import CompanyFeatureFlag
from app.models.knowledge import KBDocument
from app.models.p3_governance import MarketplacePackage
from app.models.workflow import WorkflowDefinition, WorkflowNode, \
    WorkflowStatus
from app.services.industry_pack_service import list_builtins


async def _publish(client, user, pack_id):
    return await client.post("/industry-packs/publish",
                             headers=user["headers"],
                             json={"pack_id": pack_id})


async def _make_superadmin(make_user) -> dict:
    user = await make_user()
    async with AsyncSessionLocal() as db:
        from app.models.user import User as UserRow
        row = await db.get(UserRow, user["id"])
        row.is_superadmin = True
        await db.commit()
    return user


async def _install(client, headers, pid, company_id):
    return await client.post(
        f"/governance/marketplace/packages/{pid}/install"
        f"?company_id={company_id}&confirm_permissions=true",
        headers=headers)


async def test_builtin_catalog_loads_ten_packs(client):
    r = await client.get("/industry-packs",
                         headers={"Authorization": "Bearer x"})
    # 未登录会被依赖拦截；这里只验证磁盘事实源本身
    packs = list_builtins()
    assert len(packs) == 10
    ids = {p["id"] for p in packs}
    assert {"manufacturing", "ecommerce", "clinic", "logistics"} <= ids
    for p in packs:
        assert p["custom_fields"] and p["metric_defs"] and p["agent_roster"]


async def test_publish_requires_superadmin_and_idempotent(client, tenants,
                                                          make_user):
    r = await _publish(client, tenants.alice, "manufacturing")
    assert r.status_code == 403  # 非超管

    admin = await _make_superadmin(make_user)
    r = await _publish(client, admin, "manufacturing")
    assert r.status_code == 201, r.text
    assert r.json()["updated"] is False
    pid = r.json()["package_id"]

    r2 = await _publish(client, admin, "manufacturing")
    assert r2.status_code == 201 and r2.json()["updated"] is True
    assert r2.json()["package_id"] == pid  # 同 key 复用同一行

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(MarketplacePackage).where(
                MarketplacePackage.package_type == "industry_pack"))).scalars().all()
    assert len(rows) == 1
    # 目录里能列出
    r = await client.get("/industry-packs", headers=admin["headers"])
    pack = next(p for p in r.json()["packs"]
                if p["id"] == "manufacturing")
    assert pack["published"] is True


async def test_install_plants_full_industry_stack(client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    r = await _publish(client, admin, "manufacturing")
    pid = r.json()["package_id"]
    co = tenants.alice_co["id"]

    r = await _install(client, tenants.alice["headers"], pid, co)
    assert r.status_code == 201, r.text
    body = r.json()
    kinds = {}
    for art in body["artifacts"]:
        kinds[art["kind"]] = kinds.get(art["kind"], 0) + 1
    # V6.1：包升级到商业交付级后制造包为 7 员工 / 10 工作流 / 4 合规台账
    assert kinds.get("agent") == 7
    assert kinds.get("custom_field") == 16
    assert kinds.get("workflow") == 10
    assert kinds.get("metric") == 10
    assert kinds.get("kb_document") == 1
    assert kinds.get("feature_flag") == 1
    assert kinds.get("compliance_ledger") == 1

    async with AsyncSessionLocal() as db:
        agents = (await db.execute(
            select(Agent).where(Agent.company_id == co))).scalars().all()
        assert len(agents) == 7
        assert all(not a.memory_config.get("_has_model") for a in agents)
        wfs = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.company_id == co,
                WorkflowDefinition.source == "industry_pack"))).scalars().all()
        assert len(wfs) == 10
        assert all(wf.status == WorkflowStatus.DRAFT for wf in wfs)
        # AGENT 节点轮指到了真实员工
        nodes = (await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_id == wfs[0].id))).scalars().all()
        agent_nodes = [n for n in nodes if str(n.node_type.value) == "AGENT"]
        assert agent_nodes, "行业主链路必须有 AGENT 节点"
        agent_ids = {n.config["agent_id"] for n in agent_nodes}
        assert agent_ids <= {a.id for a in agents}
        fields = (await db.execute(
            select(CustomFieldDef).where(
                CustomFieldDef.company_id == co))).scalars().all()
        assert len(fields) == 16
        metrics = (await db.execute(
            select(MetricDef).where(
                MetricDef.company_id == co))).scalars().all()
        assert len(metrics) == 10 and all(m.spec is None for m in metrics)
        docs = (await db.execute(
            select(KBDocument).where(
                KBDocument.company_id == co))).scalars().all()
        assert len(docs) == 1
        flags = (await db.execute(
            select(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == co))).scalars().all()
        assert any(f.key == "industry_pack:manufacturing" for f in flags)

    # 重复安装幂等：不翻倍
    r = await _install(client, tenants.alice["headers"], pid, co)
    assert r.status_code == 201
    async with AsyncSessionLocal() as db:
        agents = (await db.execute(
            select(Agent).where(Agent.company_id == co))).scalars().all()
        assert len(agents) == 7

    # 功能开关可查可关
    r = await client.get(f"/company-features?company_id={co}",
                         headers=tenants.alice["headers"])
    flag = next(f for f in r.json()["features"]
                if f["key"] == "industry_pack:manufacturing")
    assert flag["enabled"] is True
    r = await client.put(
        f"/company-features?company_id={co}",
        headers=tenants.alice["headers"],
        json={"key": "industry_pack:manufacturing", "enabled": False})
    assert r.status_code == 200

    # 待接线指标求值：如实提示而不是编数
    r = await client.post(
        f"/metrics/oee/evaluate?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 404 and "数据源口径" in r.text


async def test_rollback_removes_planted_artifacts(client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    r = await _publish(client, admin, "manufacturing")
    pid = r.json()["package_id"]
    co = tenants.alice_co["id"]
    r = await _install(client, tenants.alice["headers"], pid, co)
    assert r.status_code == 201

    async with AsyncSessionLocal() as db:
        from app.models.p3_governance import PackageInstallation
        inst = (await db.execute(
            select(PackageInstallation).where(
                PackageInstallation.company_id == co))).scalars().first()
        inst_id = inst.id
    r = await client.post(
        f"/governance/installations/{inst_id}/rollback",
        headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["removed_artifacts"] > 0

    async with AsyncSessionLocal() as db:
        agents = (await db.execute(
            select(Agent).where(Agent.company_id == co))).scalars().all()
        fields = (await db.execute(
            select(CustomFieldDef).where(
                CustomFieldDef.company_id == co))).scalars().all()
        docs = (await db.execute(
            select(KBDocument).where(
                KBDocument.company_id == co))).scalars().all()
        flags = (await db.execute(
            select(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == co))).scalars().all()
    assert agents == [] and fields == [] and docs == [] and flags == []
