"""角色档案出厂验收标准的继承（M6）。

角色档案（`RoleProfile.default_gate`）描述"这个角色交出来的东西该长什么样"。
工作流节点没写 gate 时继承它，避免每个流程都手工抄一遍同一套断言。

三条硬规矩（不是可选项）：

1. **节点显式配置永远优先**。继承只在 `config.gate` 缺失/为空时发生；
2. **继承来的闸门强制 `on_fail=observe`、`max_rework=0`**。角色档案是"建议标准"，
   改一次档案会影响所有继承它的流程——不允许它直接阻断生产，先观察误杀率；
3. **未知断言一律丢弃**。档案里写错一个规则名，不该让整条流水线卡死。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.team import RoleProfile
from app.models.workflow import NodeType, WorkflowNode

from .rules import KNOWN_RULES

# 继承态是"观察"：命中不阻断，只落证据。人工确认误杀率后，再到具体节点上写 fail。
INHERITED_ON_FAIL = "observe"
INHERITED_MAX_REWORK = 0


def sanitize_default_gate(raw) -> dict | None:
    """把角色档案里的 default_gate 清洗成可执行的"观察态"闸门。"""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    acc = str(raw.get("acceptance") or "").strip()
    if acc:
        out["acceptance"] = acc[:1200]
    asserts = raw.get("assert") if "assert" in raw else raw.get("asserts")
    if isinstance(asserts, dict):
        clean = {str(k): v for k, v in asserts.items() if k in KNOWN_RULES}
        if clean:
            out["assert"] = clean
    if not out:
        return None
    out["on_fail"] = INHERITED_ON_FAIL
    out["max_rework"] = INHERITED_MAX_REWORK
    return out


async def gates_for_agents(db: AsyncSession, agent_ids: list[str]) -> dict[str, dict]:
    """agent_id → 继承闸门（只包含真的有可用 default_gate 的员工）。"""
    ids = [str(a) for a in agent_ids if a]
    if not ids:
        return {}
    rows = (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()
    pids = [a.role_profile_id for a in rows if a.role_profile_id]
    if not pids:
        return {}
    profs = (await db.execute(
        select(RoleProfile).where(RoleProfile.id.in_(pids)))).scalars().all()
    profile_by_id = {p.id: p for p in profs}

    out: dict[str, dict] = {}
    for a in rows:
        if not a.role_profile_id:
            continue
        p = profile_by_id.get(a.role_profile_id)
        gate = sanitize_default_gate(p.default_gate if p else None)
        if gate:
            out[a.id] = gate
    return out


def _has_gate(cfg: dict | None) -> bool:
    return bool(isinstance(cfg, dict) and cfg.get("gate"))


async def inherit_gates_for_nodes(db: AsyncSession, nodes: list[WorkflowNode]) -> int:
    """给缺 gate 的 AGENT 节点补上角色档案的出厂标准，返回补了几个。

    直接改 `node.config`（JSON 列必须整体重新赋值才会被 ORM 识别为变更）。
    """
    agent_ids = [str((n.config or {}).get("agent_id") or "")
                 for n in nodes if n.node_type == NodeType.AGENT]
    gate_map = await gates_for_agents(db, agent_ids)
    applied = 0
    for n in nodes:
        if n.node_type != NodeType.AGENT:
            continue
        cfg = dict(n.config or {})
        if _has_gate(cfg):
            continue
        gate = gate_map.get(str(cfg.get("agent_id") or ""))
        if not gate:
            continue
        n.config = {**cfg, "gate": gate}
        applied += 1
    return applied


async def inherit_gates_for_graph(db: AsyncSession, graph: dict) -> int:
    """CEO 生成图（节点是 dict）的同一套继承；返回补了几个。"""
    nodes = [n for n in (graph.get("nodes") or [])
             if isinstance(n, dict) and n.get("type") == "AGENT"]
    gate_map = await gates_for_agents(
        db, [str(n.get("agent_id") or "") for n in nodes])
    applied = 0
    for n in nodes:
        if n.get("gate"):
            continue
        gate = gate_map.get(str(n.get("agent_id") or ""))
        if not gate:
            continue
        n["gate"] = gate
        applied += 1
    return applied
