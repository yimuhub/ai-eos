"""数据库 → spec。导出必须**稳定**，否则进 git 全是噪声 diff。

稳定性靠三件事：
1. 节点按 key 排序、连线按 (from, to, label) 排序——不依赖插入顺序；
2. 字段输出顺序固定（Python dict 保序），空值一律省略而不是写 null；
3. 位置坐标整数值输出成 int（`310.0` → `310`），避免浮点抖动。

导出内容刻意**不含内部 id**（除 agent_id 这种"解析用的锚点"）：
工作流定义本身可跨公司复用，员工 id 跨公司无意义，所以同时给出姓名。
"""
from __future__ import annotations

import json

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode

from .schema import SPEC_VERSION
from .validator import slugify

# 这几类节点的运行时配置原样外露（作者本来就手写过）
_CFG_NODE_TYPES = {"HTTP", "TOOL", "TRANSFORM"}
# 这些 config 键是内部实现，不写进 spec
_INTERNAL_KEYS = {"gate", "interpolate", "var_aliases", "output_alias"}


def _num(v) -> int | float:
    f = float(v or 0)
    return int(f) if f.is_integer() else round(f, 2)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _node_to_spec(node: WorkflowNode,
                  agents: dict[str, tuple[str, int]],
                  sub_keys: dict[str, str]) -> dict:
    cfg = dict(node.config or {})
    out: dict = {"key": node.node_key, "type": node.node_type.value,
                 "name": node.name}
    if node.position_x or node.position_y:
        out["x"] = _num(node.position_x)
        out["y"] = _num(node.position_y)

    ntype = node.node_type.value
    if ntype == "AGENT":
        if cfg.get("instruction"):
            out["task"] = cfg["instruction"]
        agent_id = cfg.get("agent_id")
        if agent_id:
            aname, dup = agents.get(str(agent_id), ("", 0))
            if aname:
                out["agent"] = aname
            # 只有"查不到名字"或"重名"时才带 agent_id 做消歧：
            # 正常情况导出只留姓名，spec 才能跨公司复用、可读。
            if (not aname) or dup > 1:
                out["agent_id"] = str(agent_id)
        if cfg.get("output_alias"):
            out["output"] = cfg["output_alias"]
    elif ntype == "CONDITION":
        out["keyword"] = cfg.get("keyword") or ""
    elif ntype == "WAIT":
        out["seconds"] = int(cfg.get("seconds") or 5)
    elif ntype == "APPROVAL":
        if cfg.get("approver_hint"):
            out["approver_hint"] = cfg["approver_hint"]
    elif ntype == "HUMAN_INPUT":
        if cfg.get("prompt"):
            out["prompt"] = cfg["prompt"]
    elif ntype == "SUB_WORKFLOW":
        wid = cfg.get("workflow_id")
        if wid:
            # 有 spec key 就导出 key（跨公司可复用）；没有才退回内部 id
            skey = sub_keys.get(str(wid))
            if skey:
                out["workflow_key"] = skey
            else:
                out["workflow_id"] = str(wid)
    elif ntype in _CFG_NODE_TYPES:
        exposed = {k: v for k, v in cfg.items() if k not in _INTERNAL_KEYS}
        if exposed:
            out["config"] = exposed

    if cfg.get("gate"):
        out["gate"] = cfg["gate"]
    return _clean(out)


async def serialize_workflow(db: AsyncSession,
                             wf: WorkflowDefinition) -> dict:
    """把一张工作流还原成 spec（幂等：同样的库内容 -> 同样的字节）。"""
    nodes_q = await db.execute(
        select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id))
    nodes = sorted(nodes_q.scalars().all(), key=lambda n: n.node_key)

    edges_q = await db.execute(
        select(WorkflowEdge).where(WorkflowEdge.workflow_id == wf.id))
    edges = sorted(edges_q.scalars().all(),
                   key=lambda e: (e.from_node, e.to_node, e.label or ""))

    agent_ids = {str((n.config or {}).get("agent_id")) for n in nodes
                 if (n.config or {}).get("agent_id")}
    agents: dict[str, tuple[str, int]] = {}
    if agent_ids:
        aq = await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(list(agent_ids))))
        rows = [(str(aid), aname) for aid, aname in aq.all()]
        counts: dict[str, int] = {}
        for _, aname in rows:
            counts[aname] = counts.get(aname, 0) + 1
        agents = {aid: (aname, counts.get(aname, 1)) for aid, aname in rows}

    sub_ids = {str((n.config or {}).get("workflow_id")) for n in nodes
               if (n.config or {}).get("workflow_id")}
    sub_keys: dict[str, str] = {}
    if sub_ids:
        sq = await db.execute(
            select(WorkflowDefinition.id, WorkflowDefinition.meta).where(
                WorkflowDefinition.id.in_(list(sub_ids))))
        for sid, smeta in sq.all():
            skey = (smeta or {}).get("spec_key")
            if skey:
                sub_keys[str(sid)] = str(skey)

    meta = wf.meta or {}
    spec: dict = {
        "spec": SPEC_VERSION,
        "key": meta.get("spec_key") or slugify(wf.name),
        "name": wf.name,
    }
    if wf.description:
        spec["description"] = wf.description
    if meta.get("inputs"):
        spec["inputs"] = meta["inputs"]

    spec["nodes"] = [_node_to_spec(n, agents, sub_keys) for n in nodes]
    spec["edges"] = [
        _clean({"from": e.from_node, "to": e.to_node, "label": e.label})
        for e in edges
    ]
    if meta.get("policy"):
        spec["policy"] = meta["policy"]
    return spec


def dump_spec(spec: dict, fmt: str = "yaml") -> str:
    """序列化成文本。YAML 用 block 风格、不排序 key、保留中文。"""
    fmt = (fmt or "yaml").lower()
    if fmt == "json":
        return json.dumps(spec, ensure_ascii=False, indent=2)
    return yaml.safe_dump(spec, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=1000)
