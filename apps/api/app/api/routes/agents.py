"""Agent CRUD."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.company import Company
from app.services.event_system import log_event

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentIn(BaseModel):
    company_id: str
    department_id: str | None = None
    name: str
    avatar: str | None = None
    role: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    manager_agent_id: str | None = None
    capabilities: list[str] = []      # 能力包: computer_use / web_browsing / ...
    backend: str | None = None        # 执行底座: native_llm / hermes / codex
    expert_template_id: str | None = None  # V5.0：绑定专家库专家（取其 system_prompt）
    attachments: list[dict] | None = None  # 📎 附件口：[{id,name,size}]（上传后的资产）


def _apply_capabilities(agent: Agent, capabilities: list[str], backend: str | None):
    """把能力包写入 memory_config JSON（保留已有配置）。"""
    from app.capability_packs import validate_capabilities
    packs, be = validate_capabilities(capabilities, backend)
    cfg = dict(agent.memory_config or {})
    if packs:
        cfg["capabilities"] = packs
    elif "capabilities" in cfg:
        cfg.pop("capabilities")
    if backend is not None:
        if be != "native_llm":
            cfg["backend"] = be
        else:
            cfg.pop("backend", None)
    agent.memory_config = cfg


class ModelBindingIn(BaseModel):
    role: str = "primary"  # primary|secondary|fallback
    model_id: str


async def _verify_agent_access(db: AsyncSession, user_id: str, agent: Agent) -> Company:
    c = await db.get(Company, agent.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Agent not found")
    return c


@router.get("")
async def list_agents(company_id: str | None = None, department_id: str | None = None,
                      status: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = select(Agent).join(Company, Company.id == Agent.company_id).where(
        Company.owner_id == user.id)
    if company_id:
        q = q.where(Agent.company_id == company_id)
    if department_id:
        q = q.where(Agent.department_id == department_id)
    if status:
        q = q.where(Agent.status == AgentStatus(status))
    result = await db.execute(q)
    agents = result.scalars().all()
    out = []
    for a in agents:
        bindings = await db.execute(
            select(AgentModelConfig).where(AgentModelConfig.agent_id == a.id))
        a_dict = {c.key: getattr(a, c.key) for c in a.__table__.columns}
        a_dict["model_configs"] = [
            {"role": b.role, "model_id": b.model_id} for b in bindings.scalars()]
        out.append(a_dict)
    return out


@router.post("", status_code=201)
async def create_agent(data: AgentIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    payload = data.model_dump()
    capabilities = payload.pop("capabilities", [])
    backend = payload.pop("backend", None)
    attachments = payload.pop("attachments", None)  # 📎 附件口（Agent 无此列，落 memory_config）
    # V5.0：绑定专家库专家 — 未填姓名/人设时自动取专家内容回填
    # V5.0.2：专家↔技能包 — 未手选技能包时按专家推荐自动配备
    if payload.get("expert_template_id"):
        from app.models.expert_template import ExpertTemplate
        tpl = await db.get(ExpertTemplate, payload["expert_template_id"])
        if tpl and tpl.company_id == data.company_id:
            if not payload.get("name"):
                payload["name"] = tpl.name
            if not payload.get("system_prompt"):
                payload["system_prompt"] = tpl.system_prompt
            if not payload.get("description"):
                payload["description"] = tpl.description
            if not capabilities:
                capabilities = tpl.recommended_packs()
    agent = Agent(**payload, status=AgentStatus.IDLE)
    _apply_capabilities(agent, capabilities, backend)
    # 📎 附件口：附件资产 id 落到 memory_config（Agent 无独立附件列），
    # 后续员工详情页/验收可据此调出参考材料
    if attachments:
        agent.memory_config = {**(agent.memory_config or {}), "attachments": attachments}
    db.add(agent)
    await log_event(db, "agent.created", "agent", None, "user", user.id,
                    {"name": data.name, "capabilities": capabilities,
                     "backend": backend or "native_llm",
                     "expert_template_id": data.expert_template_id})
    await db.commit()
    await db.refresh(agent)

    # 周期性工作能力：自动创建一条每日自动化（给自己创建待办任务）
    if "scheduled_work" in capabilities:
        from app.models.automation import AutomationDefinition
        auto = AutomationDefinition(
            id=str(__import__("uuid").uuid4()),
            company_id=data.company_id,
            name=f"[周期] {data.name} 每日工作",
            trigger_type="TIME",
            trigger_config={"cron": "0 9 * * *"},
            steps=[{"seq": 1, "type": "create_task",
                    "params": {"title": f"[自动] {data.name} 的每日例行任务",
                               "assigned_agent_id": agent.id}}],
            mode="TEST",
        )
        db.add(auto)
        await log_event(db, "automation.auto_created", "agent", agent.id,
                        "user", user.id,
                        {"definition_id": auto.id, "trigger": "daily 09:00"})
        await db.commit()
    return agent


@router.get("/capability-packs")
async def list_capability_packs(user=Depends(get_current_user)):
    """能力包 + 底座元数据（前端创建表单从这里拉取）。"""
    from app.capability_packs import AGENT_BACKENDS, CAPABILITY_PACKS
    return {
        "packs": [{"key": k, **v} for k, v in CAPABILITY_PACKS.items()],
        "backends": [{"key": k, **v} for k, v in AGENT_BACKENDS.items()],
    }


@router.get("/{aid}")
async def get_agent(aid: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    return a


@router.patch("/{aid}")
async def update_agent(aid: str, data: dict, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    # 能力包 / 底座更新
    if "capabilities" in data or "backend" in data:
        _apply_capabilities(a, data.get("capabilities") or [],
                            data.get("backend"))
    allowed = {"name", "avatar", "role", "description", "system_prompt",
               "department_id", "manager_agent_id", "status"}
    for k, v in data.items():
        if k in allowed:
            setattr(a, k if k != "status" else "status",
                    AgentStatus(v) if k == "status" else v)
    await db.commit()
    await db.refresh(a)
    return a


@router.delete("/{aid}", status_code=204)
async def delete_agent(aid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    # 删除员工不可逆：其技能绑定、记忆、档案一并消失。
    # 走统一权限层声明危险级别，而不是就地比较 owner_id。
    from app.core.permissions import Capability, ensure_capability
    await ensure_capability(db, user.id, a.company_id, Capability.DESTRUCTIVE)
    await db.delete(a)
    await db.commit()


class BatchDeleteIn(BaseModel):
    agent_ids: list[str]


@router.post("/batch-delete")
async def batch_delete_agents(data: BatchDeleteIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """批量删除 Agent。

    每个 Agent 使用独立 savepoint：单个失败（如有任务/会议引用）不影响其他，
    失败项返回原因。有引用的 Agent 会先清理引用数据（任务改派为空、
    会议记录保留但引用置空），确保能真正删掉。
    """
    from sqlalchemy.exc import IntegrityError, DBAPIError

    user_id = user.id  # 提前缓存，避免 rollback 过期后触发同步懒加载
    # 预校验权限：一次性查出该用户公司下所有 agent id，循环内不再触发 ORM 懒加载
    from app.models.company import Company as CompanyM
    q = await db.execute(
        select(Agent.id).join(CompanyM, CompanyM.id == Agent.company_id)
        .where(CompanyM.owner_id == user.id))
    owned_ids = set(q.scalars().all())

    deleted, failed = [], []
    for aid in data.agent_ids:
        if aid not in owned_ids:
            failed.append({"id": aid, "reason": "不存在或无权限"})
            continue
        a = (await db.execute(select(Agent).where(Agent.id == aid))).scalar_one_or_none()
        if not a:
            failed.append({"id": aid, "reason": "不存在"})
            continue
        try:
            async with db.begin_nested():  # savepoint：单个失败可回滚到此处
                # 1) 下属 Agent 的上级引用置空
                sub = await db.execute(select(Agent).where(
                    Agent.manager_agent_id == aid))
                for child in sub.scalars():
                    child.manager_agent_id = None
                # 2) 清理各处引用，保证外键不阻塞
                await db.execute(text(
                    "UPDATE tasks SET assigned_agent_id = NULL WHERE assigned_agent_id = :aid"),
                    {"aid": aid})
                await db.execute(text(
                    "DELETE FROM agent_model_configs WHERE agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "DELETE FROM meeting_participants WHERE agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE meetings SET coordinator_agent_id = NULL, "
                    "current_speaker_id = NULL WHERE coordinator_agent_id = :aid "
                    "OR current_speaker_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE meeting_action_items SET assignee_agent_id = NULL "
                    "WHERE assignee_agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE meeting_decisions SET proposed_by_agent_id = NULL "
                    "WHERE proposed_by_agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE meeting_messages SET speaker_agent_id = NULL "
                    "WHERE speaker_agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE assets SET agent_id = NULL WHERE agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE projects SET manager_agent_id = NULL "
                    "WHERE manager_agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "UPDATE agent_factory_jobs SET agent_id = NULL "
                    "WHERE agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "DELETE FROM agent_skills WHERE agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "DELETE FROM execution_checkpoints USING agent_executions "
                    "WHERE execution_checkpoints.execution_id = agent_executions.id "
                    "AND agent_executions.agent_id = :aid"), {"aid": aid})
                await db.execute(text(
                    "DELETE FROM agent_executions WHERE agent_id = :aid"), {"aid": aid})
                # 3) 删除本体（其余关联表为 CASCADE 自动清理）
                await db.delete(a)
            deleted.append(aid)
        except (IntegrityError, DBAPIError) as e:
            failed.append({"id": aid, "reason": f"数据库约束: {str(getattr(e, 'orig', e))[:120]}"})
            await db.rollback()
            db.expire_all()  # 回滚后 ORM 对象已过期，避免后续触发同步 IO
        except HTTPException:
            failed.append({"id": aid, "reason": "无权限"})
    if deleted:
        await log_event(db, "agent.batch_deleted", "agent", None, "user", user_id,
                        {"count": len(deleted)})
    try:
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": deleted, "deleted_count": len(deleted), "failed": failed}


class BatchCreateIn(BaseModel):
    company_id: str
    department_id: str | None = None
    manager_agent_id: str | None = None
    model_id: str | None = None
    agents: list[dict]  # [{name, role?, system_prompt?}, ...]


@router.post("/{aid}/reflect")
async def reflect_agent(aid: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """手动触发一次自我反思：归纳近期经历 → 写入长期记忆。"""
    from app.reflection_engine import self_reflect

    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    r = await self_reflect(db, a)
    return r


@router.post("/batch-create", status_code=201)
async def batch_create_agents(data: BatchCreateIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """批量创建 Agent，可统一指定部门/上级/模型。"""
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")

    created = []
    # V5.0：预取专家库（按 slug / id），供批量项按 expert_slug 或 expert_template_id 绑定专家
    # V5.0.2：专家↔技能包 — 批量项未指定技能包时按专家推荐自动配备
    from app.models.expert_template import ExpertTemplate
    wanted_slugs = [it.get("expert_slug") for it in data.agents if it.get("expert_slug")]
    wanted_ids = [it.get("expert_template_id") for it in data.agents if it.get("expert_template_id")]
    slug_map: dict[str, ExpertTemplate] = {}
    id_map: dict[str, ExpertTemplate] = {}
    if wanted_slugs or wanted_ids:
        conds = [ExpertTemplate.company_id == data.company_id,
                 ExpertTemplate.enabled.is_(True)]
        ors = []
        if wanted_slugs:
            ors.append(ExpertTemplate.source_slug.in_(wanted_slugs))
        if wanted_ids:
            ors.append(ExpertTemplate.id.in_(wanted_ids))
        # 至少一个条件成立（want slugs 或 ids），构造 OR
        where = conds + ([ors[0]] if len(ors) == 1 else [or_(*ors)])
        rows = (await db.execute(
            select(ExpertTemplate).where(*where)
        )).scalars().all()
        for r in rows:
            slug_map[r.source_slug] = r
            id_map[r.id] = r
    for item in data.agents:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        tpl = slug_map.get(item.get("expert_slug") or "")
        if not tpl:
            tpl = id_map.get(item.get("expert_template_id") or "")
        agent = Agent(
            company_id=data.company_id,
            department_id=item.get("department_id") or data.department_id,
            manager_agent_id=item.get("manager_agent_id") or data.manager_agent_id,
            name=name,
            role=item.get("role"),
            description=item.get("description") or (tpl.description if tpl else None),
            system_prompt=item.get("system_prompt") or (tpl.system_prompt if tpl else None),
            expert_template_id=item.get("expert_template_id") or (tpl.id if tpl else None),
            status=AgentStatus.IDLE,
        )
        # 专家↔技能包：项里没带 capabilities 且绑了专家 → 按专家推荐配备
        item_caps = item.get("capabilities") or []
        if not item_caps and tpl:
            item_caps = tpl.recommended_packs()
        if item_caps:
            _apply_capabilities(agent, item_caps, None)
        db.add(agent)
        created.append((agent, item))
    await db.flush()

    # 统一绑定模型
    if data.model_id:
        from app.models.agent import AgentModelConfig as AMC
        for agent, _ in created:
            db.add(AMC(agent_id=agent.id, role="primary", model_id=data.model_id))

    await log_event(db, "agent.batch_created", "agent", None, "user", user.id,
                    {"count": len(created)})
    await db.commit()
    for agent, _ in created:
        await db.refresh(agent)
    return {
        "created": [{"id": a.id, "name": a.name, "role": a.role} for a, _ in created],
        "created_count": len(created),
    }


class SetManagerIn(BaseModel):
    manager_agent_id: str | None  # None 表示清除上级


@router.put("/{aid}/manager")
async def set_manager(aid: str, data: SetManagerIn,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """设置/更换 Agent 的上级（组织架构汇报关系）。"""
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)

    if data.manager_agent_id:
        if data.manager_agent_id == aid:
            raise HTTPException(400, "不能把自己设为上级")
        mgr = await db.get(Agent, data.manager_agent_id)
        if not mgr:
            raise HTTPException(404, "上级 Agent 不存在")
        await _verify_agent_access(db, user.id, mgr)
        # 防环：沿上级链向上走，若遇到自己则拒绝
        seen, cur = {aid}, data.manager_agent_id
        while cur:
            if cur in seen:
                raise HTTPException(400, "不允许形成上下级循环")
            seen.add(cur)
            row = await db.get(Agent, cur)
            cur = row.manager_agent_id if row else None

    old_mgr = a.manager_agent_id
    a.manager_agent_id = data.manager_agent_id
    await log_event(db, "agent.manager_changed", "agent", aid, "user", user.id,
                    {"from": old_mgr, "to": data.manager_agent_id})
    await db.commit()
    return {"id": aid, "manager_agent_id": a.manager_agent_id}


@router.get("/{aid}/subordinates")
async def list_subordinates(aid: str, db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    """获取某 Agent 的全部直接下属。"""
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    q = await db.execute(select(Agent).where(Agent.manager_agent_id == aid))
    return [{"id": s.id, "name": s.name, "role": s.role, "status": s.status.value
             if hasattr(s.status, "value") else str(s.status)} for s in q.scalars()]


class BatchAssignManagerIn(BaseModel):
    assignments: list[dict]  # [{agent_id, manager_agent_id|null}]


@router.post("/batch-assign-manager")
async def batch_assign_manager(data: BatchAssignManagerIn,
                               db: AsyncSession = Depends(get_db),
                               user=Depends(get_current_user)):
    """批量设置上下级关系。"""
    ok, failed = 0, []
    for item in data.assignments:
        aid = item.get("agent_id")
        mid = item.get("manager_agent_id")  # 可为 None 清除
        a = await db.get(Agent, aid)
        if not a:
            failed.append({"agent_id": aid, "reason": "not found"})
            continue
        try:
            await _verify_agent_access(db, user.id, a)
            if mid:
                if mid == aid:
                    raise ValueError("不能把自己设为上级")
                mgr = await db.get(Agent, mid)
                if not mgr:
                    raise ValueError("上级不存在")
                await _verify_agent_access(db, user.id, mgr)
                seen, cur = {aid}, mid
                while cur:
                    if cur in seen:
                        raise ValueError("不允许形成上下级循环")
                    seen.add(cur)
                    row = await db.get(Agent, cur)
                    cur = row.manager_agent_id if row else None
            a.manager_agent_id = mid
            ok += 1
        except (HTTPException, ValueError) as e:
            failed.append({"agent_id": aid, "reason": str(e)})
    await db.commit()
    return {"updated": ok, "failed": failed}


@router.post("/{aid}/models", status_code=201)
async def bind_model(aid: str, data: ModelBindingIn,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    binding = AgentModelConfig(agent_id=aid, role=data.role, model_id=data.model_id)
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return binding


class SwitchModelIn(BaseModel):
    model_id: str  # 新的模型 ID


@router.put("/{aid}/switch-model", status_code=200)
async def switch_model(aid: str, data: SwitchModelIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """切换 Agent 的主模型（替换 existing primary binding）。"""
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)

    # 删除旧的 primary binding
    old = await db.execute(
        select(AgentModelConfig).where(
            AgentModelConfig.agent_id == aid,
            AgentModelConfig.role == "primary"))
    existing = old.scalars().first()
    if existing:
        await db.delete(existing)

    # 插入新的 primary binding
    new_binding = AgentModelConfig(agent_id=aid, role="primary", model_id=data.model_id)
    db.add(new_binding)
    await db.commit()
    await db.refresh(new_binding)
    return {"role": new_binding.role, "model_id": new_binding.model_id}


@router.get("/{aid}/models")
async def list_models_for_agent(aid: str, db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    a = await db.get(Agent, aid)
    if not a:
        raise HTTPException(404, "Agent not found")
    await _verify_agent_access(db, user.id, a)
    q = await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == aid))
    return [{"role": b.role, "model_id": b.model_id} for b in q.scalars()]
