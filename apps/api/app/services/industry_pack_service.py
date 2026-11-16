"""行业包服务（V6 M4）：一键给公司铺一套行业操作系统。

行业包 = 精选积木 + 行业图纸（docs/升级方案 §4.3）：
* custom_fields  → custom_field_defs（行业实体扩展字段）
* agent_roster   → 真实数字员工（带能力包，不绑模型——首次运行再选，
                    与 demo seed 同口径；按公司+姓名幂等）
* workflow_templates → WorkflowDefinition（DRAFT 状态，AGENT 节点按顺序
                    轮指 roster 员工；老板审完再发布）
* metric_defs    → MetricDef（spec 为空 = 口径已定义、数据源待接线，
                    求值时如实提示）
* kb_seed_topics + terms → 一篇全局知识文档（进两级 RAG）
* feature flag   → company_feature_flags: industry_pack:<id>

内容事实源在 app/industry_packs/packs/*.json（十个行业，发布后进
MarketplacePackage 目录，安装走统一市场路径，产物记 artifacts 可回滚）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger

logger = get_logger(__name__)

PACKS_DIR = Path(__file__).resolve().parent.parent / "industry_packs" / "packs"
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_STEP_TYPES = ("agent_task", "approval", "condition")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def list_builtins() -> list[dict]:
    """扫描内置行业包目录，返回校验过的内容清单。"""
    packs = []
    if not PACKS_DIR.is_dir():
        return packs
    for path in sorted(PACKS_DIR.glob("*.json")):
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            validate_content(content)
            packs.append(content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("行业包 %s 内容非法，已跳过：%s", path.name, exc)
    return packs


def get_builtin(pack_id: str) -> dict | None:
    for pack in list_builtins():
        if pack["id"] == pack_id:
            return pack
    return None


def validate_content(pack: dict) -> dict:
    """校验行业包内容（发布前调用；返回原内容）。"""
    if not isinstance(pack, dict):
        raise ValueError("行业包内容必须是对象")
    for field in ("id", "name", "version", "description"):
        if not str(pack.get(field) or "").strip():
            raise ValueError(f"缺少必填字段 {field}")
    if not PACK_ID_RE.match(pack["id"]):
        raise ValueError(f"包 id 非法：{pack['id']!r}")
    if not isinstance(pack.get("custom_fields"), list) \
            or not pack["custom_fields"]:
        raise ValueError("custom_fields 不能为空")
    if not isinstance(pack.get("workflow_templates"), list) \
            or not pack["workflow_templates"]:
        raise ValueError("workflow_templates 不能为空")
    if not isinstance(pack.get("metric_defs"), list) \
            or not pack["metric_defs"]:
        raise ValueError("metric_defs 不能为空")
    if not isinstance(pack.get("agent_roster"), list) \
            or not pack["agent_roster"]:
        raise ValueError("agent_roster 不能为空")
    for wf in pack["workflow_templates"]:
        if not str(wf.get("id") or "").strip() \
                or not str(wf.get("name") or "").strip():
            raise ValueError(f"工作流模板缺 id/name：{wf!r:.80}")
        for step in wf.get("steps") or []:
            if str(step.get("type")) not in _STEP_TYPES:
                raise ValueError(
                    f"工作流 {wf.get('id')} 步骤类型非法：{step.get('type')}")
    return pack


async def install_pack_content(db: AsyncSession, pack: dict, company_id: str,
                               user_id: str) -> list[dict]:
    """把行业包内容种进一家公司（幂等），返回产物清单（供回滚）。"""
    validate_content(pack)
    from app.models.agent import Agent, AgentStatus
    from app.models.canonical import CustomFieldDef, MetricDef
    from app.models.feature_flags import CompanyFeatureFlag
    from app.models.knowledge import KBDocument, KBDocumentStatus
    from app.models.workflow import (NodeType, WorkflowDefinition,
                                     WorkflowEdge, WorkflowNode,
                                     WorkflowStatus)

    artifacts: list[dict] = []

    # 1) 行业实体扩展字段（按 company+entity+key 幂等）
    for cf in pack["custom_fields"]:
        exists = (await db.execute(
            select(CustomFieldDef.id).where(
                CustomFieldDef.company_id == company_id,
                CustomFieldDef.entity_type == cf.get("entity"),
                CustomFieldDef.key == cf.get("key")))).first()
        if exists:
            continue
        row = CustomFieldDef(
            id=_uuid(), company_id=company_id,
            entity_type=str(cf.get("entity")), key=str(cf.get("key")),
            label=str(cf.get("label"))[:200],
            type=str(cf.get("type") or "text"),
            options=cf.get("options") or [],
            required=bool(cf.get("required")),
            description=cf.get("description"), created_by=user_id)
        db.add(row)
        await db.flush()
        artifacts.append({"kind": "custom_field", "id": row.id,
                          "name": f"{cf.get('entity')}.{cf.get('key')}"})

    # 2) 预配 Agent 编制（按 company+name 幂等；不绑模型）
    agents: list[Agent] = []
    for spec in pack["agent_roster"]:
        exists = (await db.execute(
            select(Agent).where(Agent.company_id == company_id,
                                Agent.name == spec.get("name")))).scalars().first()
        if exists:
            agents.append(exists)
            continue
        agent = Agent(company_id=company_id, name=str(spec.get("name")),
                      role=str(spec.get("department") or "员工"),
                      description=spec.get("mission"),
                      status=AgentStatus.IDLE,
                      memory_config={
                          "capabilities": [c for c in spec.get("capabilities") or []
                                           if c in ("computer_use", "web_browsing",
                                                    "persistent_memory",
                                                    "self_learning",
                                                    "scheduled_work")]})
        db.add(agent)
        await db.flush()
        agents.append(agent)
        artifacts.append({"kind": "agent", "id": agent.id,
                          "name": agent.name})

    # 3) 行业工作流模板（DRAFT；AGENT 节点按顺序轮指 roster 员工）
    for idx, wf in enumerate(pack["workflow_templates"]):
        exists = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.company_id == company_id,
                WorkflowDefinition.name == str(wf.get("name"))))).scalars().first()
        if exists:
            continue
        definition = WorkflowDefinition(
            company_id=company_id,
            name=str(wf.get("name"))[:300],
            description=str(wf.get("description") or "")[:2000],
            status=WorkflowStatus.DRAFT, created_by=user_id,
            source="industry_pack",
            meta={"pack_id": pack["id"], "template_id": wf.get("id"),
                  "pack_version": pack.get("version")})
        db.add(definition)
        await db.flush()
        # 节点链：START → step×N（AGENT/CONDITION/TASK）→ END
        roster_cycle = agents or []
        node_rows = []
        node_rows.append(("s0", NodeType.START, "开始", {}, 0))
        y = 1
        for step in wf.get("steps") or []:
            stype = str(step.get("type"))
            key = f"n{y}"
            if stype == "agent_task" and roster_cycle:
                agent = roster_cycle[(y - 1) % len(roster_cycle)]
                cfg = {"agent_id": agent.id,
                       "instruction": str(step.get("detail") or ""),
                       "task_title_template": str(step.get("name") or "")}
                node_rows.append((key, NodeType.AGENT,
                                  str(step.get("name") or f"步骤{y}"), cfg, y))
            elif stype == "condition":
                node_rows.append((key, NodeType.CONDITION,
                                  str(step.get("name") or f"判断{y}"),
                                  {"keyword": str(step.get("detail") or "")[:80]},
                                  y))
            else:
                # approval 或无 roster 时的 agent_task：占位任务节点，
                # 说明写在 config（老板可换成 AGENT 节点）
                node_rows.append((key, NodeType.TASK,
                                  str(step.get("name") or f"步骤{y}"),
                                  {"instruction": str(step.get("detail") or ""),
                                   "suggested": stype}, y))
            y += 1
        end_key = f"n{y}"
        node_rows.append((end_key, NodeType.END, "结束", {}, y))
        for key, ntype, name, cfg, yy in node_rows:
            db.add(WorkflowNode(workflow_id=definition.id, node_key=key,
                                node_type=ntype, name=name[:300],
                                config=cfg,
                                position_x=float(yy % 4) * 180,
                                position_y=float(yy) * 90))
        for a, b in zip(node_rows, node_rows[1:]):
            db.add(WorkflowEdge(workflow_id=definition.id,
                                from_node=a[0], to_node=b[0]))
        await db.flush()
        artifacts.append({"kind": "workflow", "id": definition.id,
                          "name": definition.name})

    # 4) 行业指标（spec 留空 = 口径已定、数据源待接线）
    for m in pack["metric_defs"]:
        exists = (await db.execute(
            select(MetricDef.id).where(
                MetricDef.company_id == company_id,
                MetricDef.key == m.get("key")))).first()
        if exists:
            continue
        row = MetricDef(
            id=_uuid(), company_id=company_id, key=str(m.get("key")),
            name=str(m.get("name"))[:200], unit=str(m.get("unit") or "")[:20],
            direction="lower_better" if m.get("direction") == "lower_better"
            else "higher_better",
            description=m.get("description"), spec=None, created_by=user_id)
        db.add(row)
        await db.flush()
        artifacts.append({"kind": "metric", "id": row.id,
                          "name": f"{row.key}"})

    # 5) 行业知识文档（术语 + 主题索引，进两级 RAG）
    terms_text = "\n".join(f"- {k}：{'、'.join(v)}" for k, v in
                           (pack.get("terms") or {}).items())
    topics_text = "\n".join(f"- {t}" for t in pack.get("kb_seed_topics") or [])
    kb_title = f"行业知识包：{pack['name']}"
    exists_doc = (await db.execute(
        select(KBDocument).where(KBDocument.company_id == company_id,
                                 KBDocument.title == kb_title))).scalars().first()
    if not exists_doc:
        doc = KBDocument(
            id=_uuid(), company_id=company_id, title=kb_title,
            source_type="industry_pack",
            content=(f"【{pack['name']}】{pack.get('description', '')}\n\n"
                     f"## 行业术语\n{terms_text or '（无）'}\n\n"
                     f"## 待录入主题（知识包索引）\n{topics_text or '（无）'}\n"),
            status=KBDocumentStatus.READY, scope="global", agent_id=None,
            created_by=user_id)
        db.add(doc)
        await db.flush()
        artifacts.append({"kind": "kb_document", "id": doc.id,
                          "name": kb_title})

    # 6) 功能开关
    flag_key = f"industry_pack:{pack['id']}"
    exists_flag = (await db.execute(
        select(CompanyFeatureFlag.id).where(
            CompanyFeatureFlag.company_id == company_id,
            CompanyFeatureFlag.key == flag_key))).first()
    if not exists_flag:
        db.add(CompanyFeatureFlag(
            id=_uuid(), company_id=company_id, key=flag_key,
            value={"version": pack.get("version")}, enabled=True,
            updated_by=user_id, enabled_at=_now()))
        artifacts.append({"kind": "feature_flag", "id": flag_key,
                          "name": flag_key})

    await db.commit()
    logger.info("行业包已安装 company=%s pack=%s artifacts=%d",
                company_id, pack["id"], len(artifacts))
    return artifacts


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


async def uninstall_pack_artifacts(db: AsyncSession, artifacts: list[dict],
                                   company_id: str) -> int:
    """按产物清单回收（回滚走 package_installer.uninstall_package 统一入口）。"""
    from app.models.agent import Agent
    from app.models.canonical import CustomFieldDef, MetricDef
    from app.models.feature_flags import CompanyFeatureFlag
    from app.models.knowledge import KBDocument
    from app.models.workflow import WorkflowDefinition

    removed = 0
    for art in artifacts:
        kind, art_id = art.get("kind"), art.get("id")
        if kind == "agent":
            row = await db.get(Agent, art_id)
            if row is not None and row.company_id == company_id:
                await db.delete(row)
                removed += 1
        elif kind == "workflow":
            row = await db.get(WorkflowDefinition, art_id)
            if row is not None and row.company_id == company_id:
                await db.delete(row)
                removed += 1
        elif kind == "custom_field":
            row = await db.get(CustomFieldDef, art_id)
            if row is not None and row.company_id == company_id:
                await db.delete(row)
                removed += 1
        elif kind == "metric":
            row = await db.get(MetricDef, art_id)
            if row is not None and row.company_id == company_id:
                await db.delete(row)
                removed += 1
        elif kind == "kb_document":
            row = await db.get(KBDocument, art_id)
            if row is not None and row.company_id == company_id:
                await db.delete(row)
                removed += 1
        elif kind == "feature_flag":
            await db.execute(delete(CompanyFeatureFlag).where(
                CompanyFeatureFlag.company_id == company_id,
                CompanyFeatureFlag.key == art_id))
            removed += 1
    await db.commit()
    return removed


async def feature_enabled(db: AsyncSession, company_id: str, key: str) -> bool:
    from app.models.feature_flags import CompanyFeatureFlag
    row = (await db.execute(
        select(CompanyFeatureFlag).where(
            CompanyFeatureFlag.company_id == company_id,
            CompanyFeatureFlag.key == key,
            CompanyFeatureFlag.enabled.is_(True)))).scalars().first()
    return row is not None
