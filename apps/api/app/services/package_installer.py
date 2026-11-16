"""真实安装器（V6 M2）：市场包按 category×runtime 分发为基座产物。

此前 /marketplace install 只写一行 PackageInstallation（install_log 里
硬编码 ["download","verify","install"]），没有任何真实动作——"安装"是
假的。本模块让安装产生**可审计、可回滚**的真实产物：

* tool（mcp_http）    → tool_registry 行（一键导入语义：tools 列表或整端点）
* tool（mcp_stdio）   → tool_registry 行（登记可见；执行待 MCP 子进程运行时）
* connector           → tool_registry 行（source=connector，执行归 M3 SDK）
* workflow_template   → WorkflowDefinition + 节点边（带 START/END 结构校验）
* model_provider      → ModelProvider 行（无密钥登记，密钥走 BYOK 保险库）
* skill_pack          → 仅登记引用（真身随磁盘插件加载器生效——市场无签名
                          机制前不分发可执行代码，install_log 如实标注）
* industry_pack       → M4 行业包安装器（当前明确拒绝并说明）

产物清单记在 PackageInstallation.install_log["artifacts"]，卸载按清单精确
回收（工具行连带按名清授权）。
"""
from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.p3_governance import MarketplacePackage, PackageInstallation
from app.models.toolbus import AgentToolGrant, ToolRegistry
from app.toolbus import get_tool
from app.toolbus.dispatch import _assert_public_http_url
from .plugin_manifest import (ManifestError, package_key_to_prefix,
                              validate_manifest)

logger = get_logger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,119}$")
_MAX_TOOLS_PER_PACKAGE = 50


async def install_package(db: AsyncSession, pkg: MarketplacePackage,
                          company_id: str, user_id: str) -> dict:
    """安装一个市场包，返回 {installation_id, artifacts, notes}。"""
    from .plugin_manifest import LEGACY_CATEGORIES

    if pkg.package_type in LEGACY_CATEGORIES:
        # 旧四类保持"仅记账"语义（发布时就没带新 manifest）；如实标注 stub
        inst = PackageInstallation(
            id=_uuid(), company_id=company_id, package_id=pkg.id,
            version=pkg.latest_version, installed_by=user_id,
            install_log={"steps": [{"step": "record_only", "ok": True}],
                         "artifacts": [],
                         "notes": ["旧分类包仅登记，不产生基座产物"],
                         "all_ok": True})
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        return {"installation_id": inst.id, "artifacts": [],
                "notes": ["旧分类包仅登记，不产生基座产物"],
                "version": pkg.latest_version}

    manifest = validate_manifest(pkg.manifest)
    category = manifest["category"]
    runtime = manifest["runtime"]
    entry = manifest.get("entry") or {}
    risk = manifest["risk"]
    artifacts: list[dict] = []
    notes: list[str] = []

    if category == "tool" or category == "connector":
        artifacts = await _install_tools(
            db, pkg, manifest, company_id, user_id)
        if category == "connector" and runtime == "declarative":
            notes.append("配置型连接器已登记；增量拉取管道由 M3 连接器 SDK 提供")
        if category == "tool" and runtime == "mcp_stdio":
            notes.append("已登记为本公司动态工具；子进程运行时上线前"
                         "执行会明确报错，不影响目录可见")
    elif category == "workflow_template":
        artifacts, wf_note = await _install_workflow_template(
            db, pkg, manifest, company_id, user_id)
        notes.append(wf_note)
    elif category == "model_provider":
        artifacts = await _install_model_provider(
            db, manifest, company_id, user_id)
        notes.append("已登记模型供应商（无密钥）；API Key 请到 BYOK 保险库录入")
    elif category == "skill_pack":
        notes.append("技能包已登记引用；真身由磁盘插件加载器"
                     "（plugins/skills/）安装生效——市场无签名机制前不分发代码")
    elif category == "industry_pack":
        # M4：行业包安装 = 行业服务的一次事务种植（字段/员工/工作流/指标/
        # 知识包/功能开关），产物全部记 artifacts 供回滚
        from .industry_pack_service import install_pack_content
        pack = {k: v for k, v in manifest.items()
                if k in ("id", "name", "version", "description", "terms",
                         "custom_fields", "workflow_templates", "metric_defs",
                         "agent_roster", "kb_seed_topics")}
        artifacts = await install_pack_content(db, pack, company_id, user_id)
        notes.append(f"行业包「{manifest.get('name')}」已种植"
                     f"（{len(artifacts)} 项产物，工作流为草稿待发布）")

    inst = PackageInstallation(
        id=_uuid(), company_id=company_id, package_id=pkg.id,
        version=pkg.latest_version, installed_by=user_id,
        install_log={"steps": [{"step": "validate", "ok": True},
                               {"step": "install", "ok": True,
                                "category": category, "runtime": runtime}],
                     "artifacts": artifacts, "notes": notes,
                     "manifest_risk": risk, "all_ok": True})
    db.add(inst)
    await db.commit()
    await db.refresh(inst)
    logger.info("市场包已安装 company=%s package=%s artifacts=%d",
                company_id, pkg.key, len(artifacts))
    return {"installation_id": inst.id, "artifacts": artifacts,
            "notes": notes, "version": pkg.latest_version}


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


async def _install_tools(db: AsyncSession, pkg: MarketplacePackage,
                         manifest: dict, company_id: str,
                         user_id: str) -> list[dict]:
    runtime = manifest["runtime"]
    entry = manifest.get("entry") or {}
    prefix = package_key_to_prefix(pkg.key)
    artifacts: list[dict] = []

    if runtime == "mcp_stdio":
        row = ToolRegistry(
            id=_uuid(), company_id=company_id, name=prefix,
            display_name=pkg.name, description=manifest.get("description"),
            source="mcp_stdio", source_ref=None, package_key=pkg.key,
            parameters={"type": "object", "properties": {}},
            risk=manifest["risk"],
            config={"command": entry.get("command")},
            created_by=user_id)
        db.add(row)
        await db.flush()
        return [{"kind": "tool_row", "id": row.id, "name": row.name}]

    endpoint = str(entry.get("endpoint") or "").strip()
    _assert_public_http_url(endpoint, resolve=False)  # 登记档闸门
    # manifest 声明的 secret:<名> 权限 = 该包的默认凭据命名空间：
    # 未逐工具指定 auth_secret_ref 时，工具行的凭据引用从权限推导
    secret_perm = next(
        (str(p).split(":", 1)[1] for p in manifest.get("permissions") or []
         if str(p).startswith("secret:")), None)

    existing = set((await db.execute(
        select(ToolRegistry.name).where(
            ToolRegistry.company_id == company_id))).scalars().all())
    declared = manifest.get("tools") or []
    if not isinstance(declared, list):
        raise ManifestError("manifest.tools 必须是数组")
    specs = declared[:_MAX_TOOLS_PER_PACKAGE] or [
        {"name": pkg.name, "description": manifest.get("description")}]
    for t in specs:
        remote_name = str(t.get("name") or "").strip()
        if not remote_name:
            continue
        safe = re.sub(r"[^a-z0-9_-]+", "_", remote_name.lower()).strip("_")
        tool_name = f"{prefix}_{safe}"[:120]
        if tool_name in existing or get_tool(tool_name):
            continue
        secret_ref = (t.get("auth_secret_ref")
                      or entry.get("auth_secret_ref") or secret_perm)
        row = ToolRegistry(
            id=_uuid(), company_id=company_id, name=tool_name,
            display_name=t.get("display_name") or remote_name,
            description=t.get("description") or manifest.get("description"),
            source="mcp_http" if manifest["runtime"] == "mcp_http"
            else "connector",
            source_ref=remote_name, package_key=pkg.key,
            category=manifest.get("category"),
            parameters=t.get("parameters")
            or {"type": "object", "properties": {}},
            risk=manifest["risk"],
            timeout_seconds=min(max(5, int(entry.get("timeout_seconds") or 60)),
                                600),
            endpoint=endpoint, auth_secret_ref=secret_ref,
            created_by=user_id)
        db.add(row)
        await db.flush()
        existing.add(tool_name)
        artifacts.append({"kind": "tool_row", "id": row.id,
                          "name": tool_name, "remote": remote_name})
    if not artifacts:
        raise ManifestError("该包声明的工具全部撞名，没有任何新产物——"
                            "请检查包 id 或先卸载旧版本")
    return artifacts


async def _install_workflow_template(db: AsyncSession,
                                     pkg: MarketplacePackage,
                                     manifest: dict, company_id: str,
                                     user_id: str) -> tuple[list[dict], str]:
    from app.models.workflow import (NodeType, WorkflowDefinition,
                                     WorkflowEdge, WorkflowNode,
                                     WorkflowStatus)

    wf = manifest.get("workflow") or {}
    nodes = wf.get("nodes") or []
    edges = wf.get("edges") or []
    keys = {str(n.get("node_key")) for n in nodes}
    types = {str(n.get("node_type")) for n in nodes}
    if "START" not in types or "END" not in types:
        raise ManifestError("工作流模板必须有 START 与 END 节点")
    if len(keys) != len(nodes):
        raise ManifestError("工作流模板 node_key 重复")
    for e in edges:
        if str(e.get("from_node")) not in keys \
                or str(e.get("to_node")) not in keys:
            raise ManifestError(
                f"边 {e.get('from_node')}→{e.get('to_node')} 引用了不存在的节点")

    row = WorkflowDefinition(
        company_id=company_id, name=str(wf.get("name") or pkg.name)[:300],
        description=wf.get("description") or manifest.get("description"),
        status=WorkflowStatus.PUBLISHED, created_by=user_id,
        source="marketplace", meta={"package_key": pkg.key,
                                    "manifest_version": manifest["version"]})
    db.add(row)
    await db.flush()
    for n in nodes:
        nt = NodeType(str(n.get("node_type")))
        db.add(WorkflowNode(
            workflow_id=row.id, node_key=str(n["node_key"]), node_type=nt,
            name=str(n.get("name") or n["node_key"])[:300],
            config=n.get("config") or {},
            position_x=float(n.get("position_x") or 0),
            position_y=float(n.get("position_y") or 0)))
    for e in edges:
        db.add(WorkflowEdge(workflow_id=row.id,
                            from_node=str(e["from_node"]),
                            to_node=str(e["to_node"]),
                            label=e.get("label")))
    await db.flush()
    return [{"kind": "workflow", "id": row.id, "name": row.name}], \
        f"工作流「{row.name}」已创建并发布"


async def _install_model_provider(db: AsyncSession, manifest: dict,
                                  company_id: str, user_id: str) -> list[dict]:
    from app.models.model_provider import ModelProvider, ModelProviderType

    prov = manifest["entry"].get("provider") or {}
    try:
        ptype = ModelProviderType(str(prov.get("provider_type")))
    except ValueError as exc:
        raise ManifestError(
            f"未知 provider_type {prov.get('provider_type')}") from exc
    row = ModelProvider(
        id=_uuid(), company_id=company_id,
        name=str(prov.get("name"))[:120],
        provider_type=ptype, base_url=prov.get("base_url"),
        # 市场包不带密钥：api_key_env 指向环境变量名（老机制）或空，
        # 真实 Key 走 BYOK
        api_key_env=prov.get("api_key_env"),
        config=prov.get("config") or {})
    db.add(row)
    await db.flush()
    return [{"kind": "model_provider", "id": row.id, "name": row.name}]


async def uninstall_package(db: AsyncSession, inst: PackageInstallation) -> int:
    """按 install_log 里的产物清单精确回收；返回回收的产物数。

    节点/边对 workflow_definitions 有 ondelete=CASCADE，删定义即全清；
    行业包产物按 kind 分派给行业服务回收。
    """
    from app.models.workflow import WorkflowDefinition

    log = inst.install_log or {}
    artifacts = log.get("artifacts") or []
    removed = 0
    if any(a.get("kind") in ("agent", "custom_field", "kb_document",
                            "feature_flag") for a in artifacts):
        from .industry_pack_service import uninstall_pack_artifacts
        removed += await uninstall_pack_artifacts(db, artifacts,
                                                  inst.company_id)
    for art in artifacts:
        kind, art_id = art.get("kind"), art.get("id")
        if kind in ("agent", "custom_field", "kb_document", "feature_flag"):
            continue  # 行业服务已按清单回收
        if kind == "tool_row":
            row = await db.get(ToolRegistry, art_id)
            if row is not None:
                await db.execute(delete(AgentToolGrant).where(
                    AgentToolGrant.company_id == row.company_id,
                    AgentToolGrant.tool_name == row.name))
                await db.delete(row)
                removed += 1
        elif kind == "workflow":
            row = await db.get(WorkflowDefinition, art_id)
            if row is not None:
                await db.delete(row)
                removed += 1
        elif kind == "model_provider":
            from app.models.model_provider import ModelProvider
            row = await db.get(ModelProvider, art_id)
            if row is not None:
                await db.delete(row)
                removed += 1
    if removed:
        inst.rolled_back_at = _utcnow()
        log.setdefault("notes", []).append(
            f"回滚回收产物 {removed} 项（{len(artifacts)} 项声明）")
        inst.install_log = log
    await db.commit()
    return removed


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)
