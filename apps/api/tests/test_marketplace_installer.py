"""市场真实安装器（V6 M2）：manifest 校验、分类分发、产物回收。

钉住的契约：
1. 发布校验：manifest 非法（缺 endpoint / 坏分类 / id≠key / 坏权限）→ 400；
2. 安装产出真实基座产物（tool_registry 行 / 工作流定义 / 供应商行），
   install_log.artifacts 全量留痕；
3. 回滚按产物清单精确回收（工具行连带清授权）；
4. 旧四类（AGENT/SKILL/…）保持"仅记账"，不冒充真安装；
5. 权限闸门：安装/回滚 MANAGE；租户越界 404。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.p3_governance import PackageInstallation
from app.models.toolbus import ToolRegistry

TOOL_MANIFEST = {
    "id": "kingdee-bridge", "name": "金蝶桥", "version": "1.0.0",
    "category": "tool", "runtime": "mcp_http",
    "description": "通过 MCP 直连金蝶云星空",
    "permissions": ["network:open.kingdee.com", "secret:kingdee-token"],
    "risk": "medium",
    "entry": {"endpoint": "https://mcp.example.com/kingdee",
              "timeout_seconds": 45},
    "tools": [
        {"name": "list_vouchers", "description": "列凭证",
         "parameters": {"type": "object", "properties": {
             "months": {"type": "integer"}}}},
        {"name": "get_arrears", "description": "查应收账龄",
         "parameters": {"type": "object", "properties": {}}},
    ],
}

WF_MANIFEST = {
    "id": "daily-brief-flow", "name": "每日简报流", "version": "0.1.0",
    "category": "workflow_template", "runtime": "declarative",
    "description": "晨报生成模板",
    "workflow": {
        "name": "每日简报（来自市场模板）",
        "description": "聚合→整理→输出",
        "nodes": [
            {"node_key": "s", "node_type": "START", "name": "开始"},
            {"node_key": "w", "node_type": "WAIT", "name": "缓冲",
             "config": {"seconds": 1}},
            {"node_key": "e", "node_type": "END", "name": "结束"},
        ],
        "edges": [{"from_node": "s", "to_node": "w"},
                  {"from_node": "w", "to_node": "e"}],
    },
}


async def _publish(client, headers, package_type, key, name, manifest=None,
                   permissions=None):
    return await client.post(
        "/governance/marketplace/packages",
        headers=headers,
        json={"package_type": package_type, "key": key, "name": name,
              "description": name, "version": "1.0.0",
              "permissions_required": permissions or [],
              "manifest": manifest})


async def _install(client, headers, pid, company_id, confirm=True):
    return await client.post(
        f"/governance/marketplace/packages/{pid}/install"
        f"?company_id={company_id}&confirm_permissions={str(confirm).lower()}",
        headers=headers)


async def test_publish_rejects_bad_manifest(client, tenants):
    bad_cases = [
        # 缺 endpoint
        {**TOOL_MANIFEST, "entry": {}},
        # category/runtime 不配
        {**TOOL_MANIFEST, "runtime": "declarative"},
        # 坏权限命名空间
        {**TOOL_MANIFEST, "permissions": ["root"]},
        # 坏版本
        {**TOOL_MANIFEST, "version": "1.0"},
        # 坏 id
        {**TOOL_MANIFEST, "id": "Kingdee"},
    ]
    for manifest in bad_cases:
        r = await _publish(client, tenants.alice["headers"], "tool",
                           "kingdee-bridge", "金蝶桥", manifest=manifest)
        assert r.status_code == 400, f"{manifest.get('runtime')} 应被拒：{r.text}"
    # id 与 key 不一致
    r = await _publish(client, tenants.alice["headers"], "tool",
                       "other-key", "金蝶桥", manifest=TOOL_MANIFEST)
    assert r.status_code == 400 and "不一致" in r.text
    # 合法 manifest 过
    r = await _publish(client, tenants.alice["headers"], "tool",
                       "kingdee-bridge", "金蝶桥", manifest=TOOL_MANIFEST)
    assert r.status_code == 201, r.text


async def test_install_tool_package_creates_registry_rows(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await _publish(client, alice["headers"], "tool", "kingdee-bridge",
                       "金蝶桥", manifest=TOOL_MANIFEST,
                       permissions=["network:open.kingdee.com"])
    pid = r.json()["id"]

    # 未确认权限 → 引导确认，不产生安装
    r = await _install(client, alice["headers"], pid, co["id"], confirm=False)
    assert r.status_code == 201 and r.json()["installed"] is False
    # OPERATE 角色装不了（MANAGE 闸门）——用另一家公司的 owner 更直接
    r = await _install(client, tenants.bob["headers"], pid, co["id"])
    assert r.status_code == 404  # 租户越界统一 404

    r = await _install(client, alice["headers"], pid, co["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["installed"] is True
    assert len(body["artifacts"]) == 2
    names = {a["name"] for a in body["artifacts"]}
    assert names == {"kingdee-bridge_list_vouchers",
                     "kingdee-bridge_get_arrears"}

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ToolRegistry).where(
                ToolRegistry.company_id == co["id"],
                ToolRegistry.package_key == "kingdee-bridge"))).scalars().all()
    assert len(rows) == 2
    assert all(row.source == "mcp_http" for row in rows)
    assert all(row.endpoint == "https://mcp.example.com/kingdee"
               for row in rows)
    assert all(row.auth_secret_ref == "kingdee-token" for row in rows)


async def test_install_workflow_template_and_rollback(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await _publish(client, alice["headers"], "workflow_template",
                       "daily-brief-flow", "每日简报流", manifest=WF_MANIFEST)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await _install(client, alice["headers"], pid, co["id"])
    assert r.status_code == 201, r.text
    artifacts = r.json()["artifacts"]
    assert len(artifacts) == 1 and artifacts[0]["kind"] == "workflow"
    wf_id = artifacts[0]["id"]

    from app.models.workflow import WorkflowDefinition, WorkflowEdge, \
        WorkflowNode
    async with AsyncSessionLocal() as db:
        wf = await db.get(WorkflowDefinition, wf_id)
        assert wf is not None and wf.source == "marketplace"
        nodes = (await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == wf_id))).scalars().all()
        edges = (await db.execute(select(WorkflowEdge).where(
            WorkflowEdge.workflow_id == wf_id))).scalars().all()
        assert len(nodes) == 3 and len(edges) == 2

    # 回滚：真回收
    async with AsyncSessionLocal() as db:
        inst = (await db.execute(
            select(PackageInstallation).where(
                PackageInstallation.package_id == pid))).scalars().first()
        inst_id = inst.id
    r = await client.post(f"/governance/installations/{inst_id}/rollback",
                          headers=alice["headers"])
    assert r.status_code == 200 and r.json()["removed_artifacts"] == 1
    async with AsyncSessionLocal() as db:
        assert await db.get(WorkflowDefinition, wf_id) is None
        inst = await db.get(PackageInstallation, inst_id)
        assert inst.rolled_back_at is not None


async def test_install_industry_pack_plants_via_market(client, tenants):
    """M4 后行业包走市场路径真实种植（详细断言见 test_industry_packs.py）。"""
    alice, co = tenants.alice, tenants.alice_co
    manifest = {"id": "pack-manufacturing", "name": "制造业包",
                "version": "0.1.0", "category": "industry_pack",
                "runtime": "declarative", "description": "x",
                "custom_fields": [{"entity": "order", "key": "test_cf",
                                   "label": "测试字段", "type": "text"}],
                "workflow_templates": [{"id": "t1", "name": "模板流",
                                        "steps": [{"name": "s1",
                                                   "type": "agent_task",
                                                   "detail": "做一件事"}]}],
                "metric_defs": [{"key": "test_m", "name": "测试指标",
                                 "unit": "个"}],
                "agent_roster": [{"name": "测试员工", "department": "company",
                                  "capabilities": ["persistent_memory"],
                                  "mission": "测试"}]}
    r = await _publish(client, alice["headers"], "industry_pack",
                       "pack-manufacturing", "制造业包", manifest=manifest)
    assert r.status_code == 201
    r = await _install(client, alice["headers"], r.json()["id"], co["id"])
    assert r.status_code == 201, r.text
    kinds = {a["kind"] for a in r.json()["artifacts"]}
    assert {"agent", "custom_field", "workflow", "metric",
            "feature_flag"} <= kinds


async def test_legacy_package_stays_record_only(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await _publish(client, alice["headers"], "AGENT", "legacy-agent",
                       "旧式 Agent 包", manifest={"custom": "data"})
    assert r.status_code == 201
    r = await _install(client, alice["headers"], r.json()["id"], co["id"])
    assert r.status_code == 201 and r.json()["installed"] is True
    assert r.json()["artifacts"] == []
    async with AsyncSessionLocal() as db:
        inst = (await db.execute(
            select(PackageInstallation).where(
                PackageInstallation.company_id == co["id"]))).scalars().first()
    assert inst.install_log["steps"][0]["step"] == "record_only"
