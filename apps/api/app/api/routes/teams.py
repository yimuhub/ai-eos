"""角色档案与阵容锁定 API（M3-B）。

锁定是**防幻觉**手段：导入 spec 时如果带了 `loadout_id`，所有 AGENT 节点
引用的员工必须在这个阵容里，否则报 `agent_not_in_loadout`——不会出现
"模型临时编了一个公司里不存在的岗位"。
"""
import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.db.session import get_db
from app.models.agent import Agent
from app.models.task import AgentExecution, Task
from app.models.team import RoleProfile, TeamLoadout
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowExecution, WorkflowNode, WorkflowStep)
from app.spec.validator import _norm_name

router = APIRouter(tags=["teams"])

KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class RoleProfileIn(BaseModel):
    key: str
    name: str
    domain: str | None = None
    capability_tags: list[str] = []
    system_prompt: str | None = None
    default_gate: dict | None = None
    model_tier: str | None = None


class LoadoutIn(BaseModel):
    name: str
    role_keys: list[str]
    default_inputs: dict | None = None
    note: str | None = None


class LoadoutFromExecutionIn(BaseModel):
    execution_id: str
    name: str
    note: str | None = None


def _role_dict(p: RoleProfile) -> dict:
    return {"id": p.id, "key": p.key, "name": p.name, "domain": p.domain,
            "capability_tags": p.capability_tags or [],
            "model_tier": p.model_tier, "version": p.version,
            "source": p.source}


def _loadout_dict(t: TeamLoadout) -> dict:
    return {"id": t.id, "name": t.name, "roles": t.roles or [],
            "default_inputs": t.default_inputs or {}, "note": t.note,
            "created_at": t.created_at}


async def _role_profile(db: AsyncSession, user, rid: str) -> RoleProfile:
    p = await db.get(RoleProfile, rid)
    if not p:
        raise HTTPException(404, "Role profile not found")
    await assert_company_capability(db, user.id, p.company_id, Capability.VIEW)
    return p


async def _loadout(db: AsyncSession, user, tid: str,
                   cap: Capability = Capability.VIEW) -> TeamLoadout:
    t = await db.get(TeamLoadout, tid)
    if not t:
        raise HTTPException(404, "Loadout not found")
    await assert_company_capability(db, user.id, t.company_id, cap)
    return t


async def locked_role_names(db: AsyncSession, company_id: str,
                            loadout_id: str) -> set[str]:
    """loadout 允许的角色名（归一化）。导入 spec 时用它做白名单。"""
    t = await db.get(TeamLoadout, loadout_id)
    if not t or t.company_id != company_id:
        raise HTTPException(404, "loadout 不存在或不属于该公司")
    names: set[str] = set()
    keys = [str(r.get("key")) for r in (t.roles or []) if r.get("key")]
    if keys:
        q = await db.execute(select(RoleProfile).where(
            RoleProfile.company_id == company_id,
            RoleProfile.key.in_(keys)))
        for p in q.scalars():
            names.add(_norm_name(p.name))
    # 兜底：roles 里自带 name 也算（角色档案被删后旧 loadout 仍可用）
    for r in (t.roles or []):
        if r.get("name"):
            names.add(_norm_name(r["name"]))
    return names


@router.get("/role-profiles")
async def list_role_profiles(company_id: str,
                             db: AsyncSession = Depends(get_db),
                             user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    q = await db.execute(select(RoleProfile).where(
        RoleProfile.company_id == company_id).order_by(RoleProfile.created_at))
    return [_role_dict(p) for p in q.scalars()]


@router.post("/role-profiles", status_code=201)
async def create_role_profile(data: RoleProfileIn, company_id: str,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.OPERATE)
    if not KEY_RE.match(data.key):
        raise HTTPException(400, "key 必须是小写字母开头的 ASCII 标识")
    dup = await db.execute(select(RoleProfile).where(
        RoleProfile.company_id == company_id, RoleProfile.key == data.key))
    if dup.scalars().first():
        raise HTTPException(409, f"角色 key={data.key} 已存在")
    p = RoleProfile(company_id=company_id, key=data.key,
                    name=data.name[:200], domain=(data.domain or None),
                    capability_tags=data.capability_tags or [],
                    system_prompt=data.system_prompt,
                    default_gate=data.default_gate or {},
                    model_tier=data.model_tier, source="custom")
    db.add(p)
    await db.commit()
    return _role_dict(p)


@router.get("/teams")
async def list_loadouts(company_id: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    q = await db.execute(select(TeamLoadout).where(
        TeamLoadout.company_id == company_id).order_by(TeamLoadout.created_at))
    return [_loadout_dict(t) for t in q.scalars()]


@router.get("/teams/{tid}")
async def get_loadout(tid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    t = await _loadout(db, user, tid)
    return _loadout_dict(t)


@router.post("/teams", status_code=201)
async def save_loadout(data: LoadoutIn, company_id: str,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.OPERATE)
    keys = [k for k in dict.fromkeys(data.role_keys) if k]
    if not keys:
        raise HTTPException(400, "role_keys 不能为空")
    q = await db.execute(select(RoleProfile).where(
        RoleProfile.company_id == company_id, RoleProfile.key.in_(keys)))
    found = {p.key: p for p in q.scalars()}
    missing = [k for k in keys if k not in found]
    if missing:
        raise HTTPException(400, f"角色不存在：{', '.join(missing)}")
    # 存 key + 当时的名字快照：角色档案后来改名/删除，旧阵容仍可读
    roles = [{"key": k, "name": found[k].name} for k in keys]
    t = TeamLoadout(company_id=company_id, name=data.name[:200], roles=roles,
                    default_inputs=data.default_inputs or {},
                    created_by=user.id, note=data.note)
    db.add(t)
    await db.commit()
    return _loadout_dict(t)


async def _execution_agent_nodes(ex: WorkflowExecution, workflow_id: str,
                                db: AsyncSession) -> dict[str, dict]:
    """执行时看到的 AGENT 配置；有快照时绝不用当前定义补节点。"""
    snapshot = ex.definition_snapshot
    if isinstance(snapshot, dict):
        nodes = snapshot.get("nodes")
        return {
            str(n.get("node_key")): n
            for n in (nodes if isinstance(nodes, list) else [])
            if isinstance(n, dict) and n.get("node_key")
            and n.get("node_type") in (NodeType.AGENT.value, NodeType.AGENT)
        }
    # 兼容快照上线前的历史执行；仍然只抽取真实 visited 的步骤。
    q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == workflow_id,
        WorkflowNode.node_type == NodeType.AGENT))
    return {n.node_key: {"node_key": n.node_key, "config": n.config or {}}
            for n in q.scalars()}


def _new_role_key(name: str, used_keys: set[str]) -> str:
    """ASCII slug + 稳定数字后缀；中文名也得到可导入的合法 key。"""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "role"
    if not base[0].isalpha():
        base = f"role-{base}"
    base = base[:48]
    if base not in used_keys:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        key = f"{base[:64 - len(suffix)]}{suffix}"
        if key not in used_keys:
            return key
        n += 1


@router.post("/teams/from-execution", status_code=201)
async def save_loadout_from_execution(data: LoadoutFromExecutionIn,
                                      db: AsyncSession = Depends(get_db),
                                      user=Depends(get_current_user)):
    """从本次执行实际完成且未作废的 AGENT 步骤抽取阵容。

    节点归属以执行快照为准，员工以该任务实际 AgentExecution.agent_id
    为准（Task.assigned_agent_id 会被后续改派覆盖）；当前定义里新增/改派的
    员工不会混进历史阵容。
    """
    ex = await db.get(WorkflowExecution, data.execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await db.get(WorkflowDefinition, ex.workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await assert_company_capability(db, user.id, wf.company_id, Capability.OPERATE)

    agent_nodes = await _execution_agent_nodes(ex, wf.id, db)
    if not agent_nodes:
        raise HTTPException(400, "这次执行没有可抽取的 AGENT 节点")
    q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == ex.id,
        WorkflowStep.node_type == NodeType.AGENT,
        WorkflowStep.status == RunStatus.COMPLETED,
        WorkflowStep.stale_at.is_(None),
        WorkflowStep.node_key.in_(agent_nodes),
    ).order_by(WorkflowStep.started_at, WorkflowStep.created_at,
               WorkflowStep.attempt, WorkflowStep.id))
    visited = q.scalars().all()
    if not visited:
        raise HTTPException(400, "这次执行没有已完成且未作废的 AGENT 步骤")

    task_ids = {str((s.output_data or {}).get("task_id")) for s in visited
                if isinstance(s.output_data, dict) and s.output_data.get("task_id")}
    # 一个任务可能被重跑/改派；取时间最近的真实执行记录，而不是读取可变的
    # Task.assigned_agent_id。最终仍按 Agent.company_id 复核租户归属。
    execution_agents: dict[str, str] = {}
    if task_ids:
        executions_q = await db.execute(
            select(AgentExecution).join(Task, Task.id == AgentExecution.task_id).where(
                AgentExecution.task_id.in_(task_ids),
                Task.company_id == wf.company_id,
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()))
        for run in executions_q.scalars():
            execution_agents.setdefault(run.task_id, run.agent_id)

    # 保持实际访问顺序用于返回 roles；key 分配另按员工稳定排序。
    agent_order: list[str] = []
    for step in visited:
        output = step.output_data if isinstance(step.output_data, dict) else {}
        task_id = str(output.get("task_id") or "")
        if task_id:
            # 有 task_id 却没有真实执行记录时不能回退到旧配置。
            agent_id = execution_agents.get(task_id, "")
        else:
            # 旧执行的步骤没有 task_id，只能用执行时的节点配置。
            config = agent_nodes[step.node_key].get("config") or {}
            agent_id = str(config.get("agent_id") or "")
        if agent_id and agent_id not in agent_order:
            agent_order.append(agent_id)
    if not agent_order:
        raise HTTPException(400, "这次执行没有可抽取的本公司 AGENT 员工")

    agents_q = await db.execute(select(Agent).where(
        Agent.id.in_(agent_order), Agent.company_id == wf.company_id))
    agents = {a.id: a for a in agents_q.scalars()}
    agent_order = [aid for aid in agent_order if aid in agents]
    if not agent_order:
        raise HTTPException(400, "这次执行没有可抽取的本公司 AGENT 员工")

    profiles_q = await db.execute(select(RoleProfile).where(
        RoleProfile.company_id == wf.company_id))
    profiles = list(profiles_q.scalars())
    by_id = {p.id: p for p in profiles}
    # 必须预装全部既有 key（包括历史数据里的中文 key），不能只看本次新增。
    used_keys = {p.key for p in profiles}
    key_by_agent: dict[str, str] = {}
    for aid in sorted(agent_order, key=lambda aid: (agents[aid].name, aid)):
        agent = agents[aid]
        profile = by_id.get(agent.role_profile_id or "")
        if profile is None:
            key = _new_role_key(agent.name, used_keys)
            used_keys.add(key)
            profile = RoleProfile(company_id=wf.company_id, key=key,
                                  name=agent.name[:200], source="from_execution")
            db.add(profile)
            await db.flush()
            by_id[profile.id] = profile
            agent.role_profile_id = profile.id
        else:
            used_keys.add(profile.key)
        key_by_agent[aid] = profile.key

    roles: list[dict] = []
    seen_keys: set[str] = set()
    for aid in agent_order:
        key = key_by_agent[aid]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        roles.append({"key": key, "name": by_id[agents[aid].role_profile_id].name})

    t = TeamLoadout(company_id=wf.company_id, name=data.name[:200],
                    roles=roles, default_inputs={}, created_by=user.id,
                    note=data.note)
    db.add(t)
    await db.commit()
    return _loadout_dict(t)
