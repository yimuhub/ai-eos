"""spec → 数据库。导入的唯一写入口。

两条硬规则：

1. **先校验、后写库**。校验不过直接抛 `SpecInvalid`，一行都不落——
   半截入库的工作流比没有更糟（用户以为导进来了，实际是残缺图）。
2. **spec 只做出入口，DB 才是运行时真相**。导入是一次显式动作，
   导出的文件永远不会自动回写数据库。

变量插值是"按需开启"的：只有 spec 里写了 `{{...}}` 的 AGENT 节点才会在
config 上带 `interpolate=true`，存量工作流（instruction 里恰好有花括号的）
行为完全不变。
"""
from __future__ import annotations

from datetime import datetime, timezone

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.workflow import (NodeType, WorkflowDefinition, WorkflowEdge,
                                 WorkflowNode, WorkflowStatus)

from .schema import SPEC_VERSION
from .validator import Issue, SpecContext, _norm_name, errors, slugify, summary, validate

logger = get_logger(__name__)


class SpecParseError(ValueError):
    """文本（YAML/JSON）解析失败。"""


class SpecInvalid(ValueError):
    """校验不通过。带完整问题清单，接口层可直接回 422。"""

    def __init__(self, issues: list[Issue]):
        super().__init__(summary(issues) or "spec 校验失败")
        self.issues = issues

    @property
    def detail(self) -> str:
        return summary(self.issues)


def parse_spec(payload) -> dict:
    """接收 dict / YAML 文本 / JSON 文本，统一成 dict。

    YAML 的 `safe_load` 本身能解析 JSON（JSON 是 YAML 的子集），所以
    用户传哪种都行，不需要先猜格式。
    """
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str) or not payload.strip():
        raise SpecParseError("spec 内容为空")
    try:
        data = yaml.safe_load(payload)
    except yaml.YAMLError as e:
        raise SpecParseError(f"YAML/JSON 解析失败：{e}")
    if not isinstance(data, dict):
        raise SpecParseError("spec 顶层必须是映射（键值对），不是列表或标量")
    return data


async def build_context(db: AsyncSession, company_id: str) -> SpecContext:
    """从库里取校验所需的"事实"：员工名单、可见工作流。"""
    agents_q = await db.execute(
        select(Agent.id, Agent.name).where(Agent.company_id == company_id))
    names: dict[str, str] = {}
    ids: set[str] = set()
    names_by_id: dict[str, str] = {}
    for aid, aname in agents_q.all():
        ids.add(str(aid))
        names_by_id[str(aid)] = str(aname)
        key = _norm_name(aname)
        # 重名时保留第一个：导出会同时给 agent_id，不依赖名字消歧
        names.setdefault(key, str(aid))
    wf_q = await db.execute(
        select(WorkflowDefinition.id, WorkflowDefinition.meta).where(
            WorkflowDefinition.company_id == company_id))
    wf_ids: set[str] = set()
    wf_keys: dict[str, str] = {}
    for wid, wmeta in wf_q.all():
        wf_ids.add(str(wid))
        spec_key = (wmeta or {}).get("spec_key")
        if spec_key:
            # 同 key 多版本时取到的那条即可用（导入是新建版本，不覆盖）
            wf_keys.setdefault(str(spec_key), str(wid))
    return SpecContext(agent_names=names, agent_ids=ids,
                       agent_names_by_id=names_by_id,
                       workflow_ids=wf_ids, workflow_keys=wf_keys)


def validate_spec(spec: dict, ctx: SpecContext | None = None) -> list[Issue]:
    return validate(spec, ctx)


def _vars_in(text: str) -> list[str]:
    from .validator import _VAR_COMPILED
    return _VAR_COMPILED.findall(text or "")


def _resolve_agent(node: dict, ctx: SpecContext) -> str | None:
    """优先 agent_id（精确、抗重名），其次姓名。"""
    aid = node.get("agent_id")
    if isinstance(aid, str) and aid and aid in ctx.agent_ids:
        return aid
    name = node.get("agent")
    if isinstance(name, str) and name:
        return ctx.agent_names.get(_norm_name(name))
    return None


def _node_config(node: dict, *, ctx: SpecContext, aliases: dict[str, str]) -> dict:
    """把 spec 节点字段摊成运行时的 node.config。"""
    ntype = node["type"]
    cfg: dict = {}

    if ntype == "AGENT":
        agent_id = _resolve_agent(node, ctx)
        if not agent_id:
            raise SpecInvalid([Issue("agent_not_found",
                                     f"无法解析执行员工：{node.get('agent') or node.get('agent_id')}",
                                     f"nodes[{node['key']}]")])
        task = str(node.get("task") or "")
        cfg.update({
            "agent_id": agent_id,
            "task_title_template": str(node.get("name") or node["key"])[:200],
            "instruction": task[:4000],
        })
        if node.get("output"):
            cfg["output_alias"] = str(node["output"])[:64]
        refs = _vars_in(task)
        if refs:
            # 只对显式引用变量的节点开启插值：存量 instruction 行为不变
            cfg["interpolate"] = True
            cfg["var_aliases"] = {
                a: k for a, k in aliases.items() if a in {r.split(".")[0] for r in refs}
            }
    elif ntype == "CONDITION":
        cfg["keyword"] = str(node.get("keyword") or "")[:100]
    elif ntype == "WAIT":
        cfg["seconds"] = max(0, min(int(node.get("seconds") or 5), 60))
    elif ntype == "APPROVAL":
        cfg["approver_hint"] = str(node.get("approver_hint") or "")[:200]
    elif ntype == "HUMAN_INPUT":
        cfg["prompt"] = str(node.get("prompt") or "")[:500]
    elif ntype == "SUB_WORKFLOW":
        wid = node.get("workflow_id")
        if not wid:
            wkey = node.get("workflow_key")
            wid = ctx.workflow_keys.get(str(wkey)) if wkey else None
        if wid:
            cfg["workflow_id"] = str(wid)
    elif ntype in ("HTTP", "TOOL", "TRANSFORM"):
        raw = node.get("config")
        cfg.update(raw if isinstance(raw, dict) else {})

    if node.get("gate"):
        cfg["gate"] = dict(node["gate"])
    return cfg


async def compile_spec(
    db: AsyncSession,
    spec: dict,
    *,
    company_id: str,
    user_id: str,
    project_id: str | None = None,
    status: WorkflowStatus = WorkflowStatus.PUBLISHED,
    ctx: SpecContext | None = None,
) -> WorkflowDefinition:
    """校验并落库，返回新建的 WorkflowDefinition。"""
    ctx = ctx or await build_context(db, company_id)
    issues = validate(spec, ctx)
    if errors(issues):
        raise SpecInvalid(issues)

    nodes = [n for n in spec.get("nodes") or [] if isinstance(n, dict)]
    aliases = {str(n["output"]): n["key"] for n in nodes if n.get("output")}
    key = str(spec.get("key") or slugify(str(spec.get("name") or "")))

    wf = WorkflowDefinition(
        company_id=company_id,
        project_id=project_id,
        name=str(spec.get("name") or "")[:300],
        description=(str(spec.get("description") or "")[:2000] or None),
        status=status,
        created_by=user_id,
        source="import",
        meta={
            "spec_version": SPEC_VERSION,
            "spec_key": key,
            "inputs": spec.get("inputs") or [],
            "policy": spec.get("policy") or {},
            "imported_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(wf)
    await db.flush()

    for n in nodes:
        db.add(WorkflowNode(
            workflow_id=wf.id,
            node_key=str(n["key"])[:50],
            node_type=NodeType(str(n["type"])),
            name=str(n.get("name") or n["key"])[:300],
            config=_node_config(n, ctx=ctx, aliases=aliases),
            position_x=float(n.get("x") or 0),
            position_y=float(n.get("y") or 0),
        ))
    for e in spec.get("edges") or []:
        if not isinstance(e, dict):
            continue
        db.add(WorkflowEdge(
            workflow_id=wf.id,
            from_node=str(e["from"])[:50],
            to_node=str(e["to"])[:50],
            label=(str(e["label"])[:100] if e.get("label") else None),
        ))
    await db.flush()
    logger.info("workflow spec imported company=%s key=%s nodes=%d",
                company_id, key, len(nodes))
    return wf
