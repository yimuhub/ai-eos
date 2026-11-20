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
from app.models.team import RoleProfile, TeamLoadout
from app.models.workflow import NodeType, WorkflowDefinition, WorkflowNode
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


@router.post("/teams/from-execution", status_code=201)
async def save_loadout_from_execution(data: LoadoutFromExecutionIn,
                                      db: AsyncSession = Depends(get_db),
                                      user=Depends(get_current_user)):
    """把一次跑完的执行里实际参与的角色存成阵容。

    没绑角色档案的员工就地补一个（key 由姓名生成），保证"存下来的阵容
    现在就能用"，而不是要求用户先去建档案。
    """
    from app.models.workflow import WorkflowExecution

    ex = await db.get(WorkflowExecution, data.execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await db.get(WorkflowDefinition, ex.workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await assert_company_capability(db, user.id, wf.company_id, Capability.OPERATE)

    q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wf.id, WorkflowNode.node_type == NodeType.AGENT))
    nodes = q.scalars().all()
    agent_ids = [str((n.config or {}).get("agent_id")) for n in nodes
                 if (n.config or {}).get("agent_id")]
    if not agent_ids:
        raise HTTPException(400, "这次执行没有 AGENT 节点，无法抽取阵容")

    agents: list[Agent] = []
    for aid in dict.fromkeys(agent_ids):
        a = await db.get(Agent, aid)
        if a and a.company_id == wf.company_id:
            agents.append(a)

    roles: list[dict] = []
    used_keys: set[str] = set()
    for a in agents:
        profile = await db.get(RoleProfile, a.role_profile_id) if a.role_profile_id else None
        if profile is None:
            base = re.sub(r"[^a-z0-9]+", "-", a.name.lower()).strip("-") or "role"
            key = base[:48]
            n = 1
            while key in used_keys:
                n += 1
                key = f"{base[:40]}-{n}"
            profile = RoleProfile(company_id=wf.company_id, key=key,
                                  name=a.name[:200], source="from_execution")
            db.add(profile)
            await db.flush()
            a.role_profile_id = profile.id
        used_keys.add(profile.key)
        if not any(r["key"] == profile.key for r in roles):
            roles.append({"key": profile.key, "name": profile.name})

    t = TeamLoadout(company_id=wf.company_id, name=data.name[:200],
                    roles=roles, default_inputs={}, created_by=user.id,
                    note=data.note)
    db.add(t)
    await db.commit()
    return _loadout_dict(t)
