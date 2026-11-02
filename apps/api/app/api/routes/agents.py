"""Agent CRUD + P5 行级可见性与创作权。

## 三件事只在这里定义（其余入口复用同一套条件）

1. **可见性（读）**：列表/详情走 `core.scope.agent_read_scope(ctx)`，
   条件直接拼进 SQL——不允许"拉全量再在 Python 里过滤"，
   那会让分页、计数与权限边界三处各说各话。
2. **创作权（写）**：更新/删除/换模型/上下级统一走
   `ensure_agent_access(...)`，按操作给 VIEW / OPERATE / MANAGE / DESTRUCTIVE。
   失败一律 404（403 等于告诉调用方"这个 id 存在"）。
3. **配额**：建 Agent 前按「本人 → 目标部门 → 公司默认」取第一个非 None 的上限，
   超限不硬失败，而是落一条 `ApprovalRequest` 并返回 409，把决定权交回人。

`visibility` 的默认值是 `department`：多成员时代，"新建即全公司可见"等于
默认泄露；老数据为 NULL 的仍按 company 解释（见 scope.py）。
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import Capability, ensure_capability, role_has
from app.core.scope import (
    VISIBILITY_COMPANY,
    VISIBILITY_DEPARTMENT,
    VISIBILITY_PRIVATE,
    agent_read_scope,
    agent_write_scope,
    build_ctx,
    ensure_agent_access,
    ensure_dept_access,
)
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.company import Company, Department
from app.models.membership import CompanyMember, DepartmentMember
from app.models.user import User
from app.schemas.responses import (AgentBatchAssignManagerOut,
                                   AgentBatchCreateOut, AgentBatchDeleteOut,
                                   AgentCardOut, AgentListItemOut,
                                   AgentManagerOut, AgentModelBindingOut,
                                   AgentModelConfigOut, AgentOut,
                                   AgentSkillItemOut, AgentSkillProvisionOut,
                                   AgentSubordinateOut, AgentSwitchModelOut,
                                   CapabilityPacksOut)
from app.services import license_service
from app.services.event_system import log_event
from app.services.usage_service import count_active_agents

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_VISIBILITIES = (VISIBILITY_COMPANY, VISIBILITY_DEPARTMENT,
                      VISIBILITY_PRIVATE)


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
    # company | department | private；缺省 department（见模块说明）
    visibility: str | None = None


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


async def _load_agent(db: AsyncSession, agent_id: str) -> Agent:
    """按 id 取行；不存在统一 404（不泄露存在性）。"""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


async def _verify_agent_access(db: AsyncSession, user_id: str, agent: Agent,
                               capability: Capability = Capability.VIEW) -> Company:
    """**兼容层**：保留 user_id 形态给尚未迁移的调用方（如 offboarding）。

    新代码请直接用 `ensure_agent_access(db, user, agent, capability)`——
    它拿得到 `is_superadmin` 等身份信息。这里只按行级范围判定，
    写/删的"有多重"仍由调用方显式声明的 capability 决定。
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    await ensure_agent_access(db, user, agent, capability)
    company = await db.get(Company, agent.company_id)
    if company is None:
        raise HTTPException(404, "Not found")
    return company


async def _user_company_ids(db: AsyncSession, user) -> list[str]:
    """当前用户"有身份"的公司集合（所有权 / 生效成员 / 部门成员）。

    列表端点在没有显式 company_id 时要跨公司取数，先把候选公司收敛成
    一条 SQL，再逐公司用 `build_ctx` 生成行级条件——**不**先把 Agent 拉出来
    再在 Python 里判断。
    """
    # 候选公司枚举（不是权限判定）：真正的门禁是逐公司的 build_ctx / resolve_role，
    # 这里只回答"他可能在哪几家公司里有身份"。
    owned = select(Company.id).where(Company.owner_id == str(user.id))
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user.id,
        func.lower(CompanyMember.status) == "active")
    dept = (select(Department.company_id)
            .join(DepartmentMember,
                  DepartmentMember.department_id == Department.id)
            .where(DepartmentMember.user_id == user.id))
    rows = await db.execute(union(owned, member, dept))
    return [r[0] for r in rows.all() if r[0]]


def _normalize_visibility(raw: str | None) -> str:
    """只接受 company|department|private；缺省 department，非法值 422。"""
    value = (raw or VISIBILITY_DEPARTMENT).strip().lower()
    if value not in VALID_VISIBILITIES:
        raise HTTPException(
            422, f"visibility 只能是 {'/'.join(VALID_VISIBILITIES)}")
    return value


async def _resolve_agent_quota(db: AsyncSession, company: Company,
                               user_id: str,
                               department_id: str | None) -> int | None:
    """配额取值优先级：本人（company_members）→ 目标部门 → 公司默认。

    第一个**非 None** 的值生效；全为 None 视为不限。
    `companies` 表目前没有 agent_quota 列，故用 getattr 兜底——
    将来加列即自动生效，不需要改这里。
    """
    personal = (await db.execute(
        select(CompanyMember.agent_quota).where(
            CompanyMember.company_id == company.id,
            CompanyMember.user_id == user_id).limit(1))).scalars().first()
    if personal is not None:
        return personal
    if department_id:
        dept_quota = (await db.execute(
            select(Department.agent_quota).where(
                Department.id == department_id,
                Department.company_id == company.id).limit(1))).scalars().first()
        if dept_quota is not None:
            return dept_quota
    return getattr(company, "agent_quota", None)


async def _department_agent_count(db: AsyncSession, company_id: str,
                                  department_id: str | None) -> int:
    """目标部门当前 Agent 数（未指定部门时按"公司直挂"计数）。"""
    cond = (Agent.department_id == department_id if department_id
            else Agent.department_id.is_(None))
    total = (await db.execute(
        select(func.count()).select_from(Agent).where(
            Agent.company_id == company_id, cond))).scalar()
    return int(total or 0)


async def _enforce_license_seats(db: AsyncSession, company: Company, user,
                                 *, adding: int, label: str) -> None:
    """License 席位闸门（社区模式不生效）。

    与部门 Agent 配额是**两道独立闸门**：部门配额管"某个部门能招几个"，
    License 席位管"整个公司最多几个"。两者都用审批单承接，但 action_type
    不同（agent_quota / license_seats），审批人一眼能分清该找谁扩容。
    """
    limit = license_service.seat_limit()
    if limit is None:
        return
    used = await count_active_agents(db, company.id)
    if used + adding <= limit:
        return

    overage = used + adding - limit
    from app.models.intervention import ApprovalRequest
    req = ApprovalRequest(
        id=str(uuid.uuid4()),
        company_id=company.id,
        action_type="license_seats",
        payload={"limit": limit, "used": used, "adding": adding,
                 "overage": overage, "requested_by_user_id": user.id,
                 "label": label},
        reason=(f"License 席位已用满（上限 {limit}、已用 {used}），"
                f"{label}将超出 {overage} 个，等待管理员批准"),
    )
    db.add(req)
    # 与 agent_quota 同事务：409 抛出后请求级 session 不再提交，审批单与
    # 通知必须在 raise 之前落库。
    from app.services.notification_service import notify_approval_pending
    await notify_approval_pending(
        db, company_id=company.id,
        title="待审批：License 席位不足",
        body=(f"上限 {limit}、已用 {used}，本次超出 {overage} 个；"
              f"批准后才会创建。如需扩容请联系发行方更新 License。"))
    await db.commit()
    raise HTTPException(
        409,
        detail=(f"License 席位不足：上限 {limit}、已用 {used}，"
                f"本次超出 {overage} 个。超额创建审批已提交"
                f"（审批单 {req.id}），请等待管理员批准。"))


async def _enforce_agent_quota(db: AsyncSession, company: Company, user,
                               department_id: str | None, *, adding: int = 1,
                               label: str = "Agent") -> None:
    """创建前配额闸门：超限 → 落审批单 + 409（不静默放行、不硬删除）。

    并发口径刻意只做一次 COUNT：配额是"软上限 + 人工审批"，用分布式锁
    去保证严格不超卖，代价远大于收益；多算一个的后果只是多一张审批单。

    顺序：License 只读（过期/不可信）→ 现有部门配额 → License 席位。
    社区模式（无 License）时后两步里席位闸门直接跳过，行为与改造前一致。
    """
    state = license_service.get_state()
    if state.read_only:
        raise HTTPException(
            403,
            "License 已过期，当前为只读模式"
            if state.status == "expired"
            else "License 校验未通过，当前为只读模式")

    quota = await _resolve_agent_quota(db, company, user.id, department_id)
    if quota is not None:
        current = await _department_agent_count(db, company.id, department_id)
        if current + adding > quota:
            overage = current + adding - quota
            from app.models.intervention import ApprovalRequest
            req = ApprovalRequest(
                id=str(uuid.uuid4()),
                company_id=company.id,
                action_type="agent_quota",
                payload={"department_id": department_id, "adding": adding,
                         "quota": quota, "current_count": current,
                         "overage": overage, "requested_by_user_id": user.id,
                         "label": label},
                reason=(f"部门 Agent 配额已满（上限 {quota}、现有 {current}），"
                        f"{label}将超出 {overage} 个，等待管理员批准"),
            )
            db.add(req)
            # 审批单与通知同事务提交：409 抛出后请求级 session 不再提交，
            # 所以必须在 commit 之前把通知也 add 进去。
            from app.services.notification_service import notify_approval_pending
            await notify_approval_pending(
                db, company_id=company.id,
                title=f"待审批：{label}超出 Agent 配额",
                body=(f"上限 {quota}、现有 {current}，本次超出 {overage} 个；"
                      f"批准后才会创建。"))
            # 先落审批单：409 抛出后请求级 session 不再提交，审批记录必须已经存在
            await db.commit()
            raise HTTPException(
                409,
                detail=(f"部门 Agent 配额已满：上限 {quota}、现有 {current}，"
                        f"本次超出 {overage} 个。超额创建审批已提交"
                        f"（审批单 {req.id}），请等待管理员批准。"))

    await _enforce_license_seats(db, company, user, adding=adding, label=label)


@router.get("", response_model=list[AgentListItemOut])
async def list_agents(company_id: str | None = None, department_id: str | None = None,
                      status: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """列表：行级可见条件直接拼进 SQL（见 core/scope.py 的说明）。"""
    if company_id:
        # 与该公司无关 → 404；同时这也是静态扫描认的归属守卫
        await ensure_capability(db, user.id, company_id, Capability.VIEW)
        ctx = await build_ctx(db, user, company_id)
        scope = agent_read_scope(ctx)
        q = select(Agent).where(scope)
    elif getattr(user, "is_superadmin", False):
        # 平台超管可以不属于任何公司：不加行级条件（仍可叠加 company_id 过滤）
        q = select(Agent)
    else:
        scopes = []
        for cid in await _user_company_ids(db, user):
            scopes.append(agent_read_scope(await build_ctx(db, user, cid)))
        if not scopes:
            return []
        q = select(Agent).where(or_(*scopes))
    if department_id:
        q = q.where(Agent.department_id == department_id)
    if status:
        q = q.where(Agent.status == AgentStatus(status))
    result = await db.execute(q)
    agents = result.scalars().all()
    # 一次查出全部绑定，Python 侧分组——避免每员工一次 SELECT 的 N+1
    bindings_by_agent: dict[str, list[dict]] = {}
    if agents:
        rows = await db.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id.in_([a.id for a in agents])))
        for b in rows.scalars():
            bindings_by_agent.setdefault(b.agent_id, []).append(
                {"role": b.role, "model_id": b.model_id})
    out = []
    for a in agents:
        a_dict = {c.key: getattr(a, c.key) for c in a.__table__.columns}
        a_dict["model_configs"] = bindings_by_agent.get(a.id, [])
        out.append(a_dict)
    return out


@router.post("", status_code=201, response_model=AgentOut)
async def create_agent(data: AgentIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """创建 Agent：写归属（created_by_user_id）+ 可见性 + 配额闸门。

    门禁：
    * 任何在本公司有 OPERATE 的角色都能建（member 建自己的私有 Agent、head 建本部门的）；
    * `visibility=company` 只有 owner/company_admin（scope.sees_all_agents）能给，
      否则 404——"把员工广播给全公司"是管理动作，不是成员的创作权；
    * 指定部门时必须对该部门有归属（ensure_dept_access）。
    """
    c = await db.get(Company, data.company_id)
    if not c:
        raise HTTPException(404, "Company not found")
    await ensure_capability(db, user.id, data.company_id, Capability.OPERATE)
    ctx = await build_ctx(db, user, data.company_id)

    visibility = _normalize_visibility(data.visibility)
    if visibility == VISIBILITY_COMPANY and not ctx.sees_all_agents:
        raise HTTPException(404, "Not found")
    if data.department_id:
        await ensure_dept_access(ctx, data.department_id, db=db)

    await _enforce_agent_quota(db, c, user, data.department_id,
                               label=f"Agent「{data.name}」")

    payload = data.model_dump()
    payload["visibility"] = visibility
    payload["created_by_user_id"] = user.id
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

    # 创建即配备技能：本地库命中就装；没有就按岗位生成一张技能卡；
    # 都没有时兜底通用能力。手工创建不做外网市场搜索（避免建员工要等 8 秒），
    # 市场补齐由 CEO 招人链路或「智能补齐」接口负责。
    try:
        from app.skill_provisioning import provision_agent_skills
        await provision_agent_skills(
            db, agent, hints=[data.role or "", data.system_prompt or ""],
            allow_market=False, allow_generate=True)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — 技能配备失败不影响员工已建成
        import logging
        logging.getLogger(__name__).warning(
            "手工创建 Agent 的技能配备失败 agent=%s: %s: %s",
            agent.id, type(exc).__name__, exc)

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


@router.get("/capability-packs", response_model=CapabilityPacksOut,
            response_model_exclude_none=True)
async def list_capability_packs(user=Depends(get_current_user)):
    """能力包 + 底座元数据（前端创建表单从这里拉取）。"""
    from app.capability_packs import AGENT_BACKENDS, CAPABILITY_PACKS
    return {
        "packs": [{"key": k, **v} for k, v in CAPABILITY_PACKS.items()],
        "backends": [{"key": k, **v} for k, v in AGENT_BACKENDS.items()],
    }


@router.get("/{aid}", response_model=AgentOut)
async def get_agent(aid: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.VIEW)
    return a


@router.get("/{aid}/card", response_model=AgentCardOut)
async def agent_card(aid: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """V5.8 ⑨ Agent Card（学 A2A 协议）：员工工牌 JSON。

    局域网/外部系统按工牌发现"这个员工是谁、会什么、找哪干"：
    - name/role/description：身份
    - capabilities：技能清单 + 电脑操作能力授权
    - needs_auth：哪些动作要先找老板批（A2A 任务状态机的 needs-auth 态）
    - health：当前状态
    这是"别家公司 agent 按工牌调用你的员工"的协议层基础。
    """
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.VIEW)
    # 能力清单：技能（skills 表名）+ 电脑操作授权
    from app.models.skill import AgentSkill, Skill
    from sqlalchemy import select as _sel
    skill_links = (await db.execute(
        _sel(AgentSkill.skill_id).where(AgentSkill.agent_id == a.id))
        ).scalars().all()
    cap_names: list[str] = []
    if skill_links:
        from app.models.skill import Skill as _Sk
        skill_rows = (await db.execute(
            _sel(_Sk.name).where(_Sk.id.in_(skill_links)))).scalars().all()
        cap_names = [n for n in skill_rows if n]
    # 电脑操作能力授权（公司级）
    from app.computer_agent import ComputerPermission
    granted_caps = [row for row in (await db.execute(
        _sel(ComputerPermission.capability).where(
            ComputerPermission.company_id == a.company_id,
            ComputerPermission.granted.is_(True)))).scalars().all()]
    # needs-auth 态（A2A）：该 Agent 名下挂起的审批单 → 调用方应知"在等批"
    from app.models.intervention import ApprovalRequest
    pending_auth = (await db.execute(
        _sel(ApprovalRequest.id, ApprovalRequest.action_type,
             ApprovalRequest.created_at).where(
            ApprovalRequest.requested_by_agent_id == a.id,
            ApprovalRequest.status == "PENDING")
        .order_by(ApprovalRequest.created_at.desc()).limit(5)
    )).all()
    return {
        "agent_id": a.id,
        "name": a.name,
        "role": a.role,
        "description": a.description,
        "status": a.status.value if hasattr(a.status, "value") else str(a.status),
        "department_id": a.department_id,
        "company_id": a.company_id,
        "capabilities": cap_names or None,
        "computer_capabilities": granted_caps,
        # needs-auth（A2A 状态机）：高危电脑操作默认要审批
        "needs_auth": {
            "high_risk_actions": ["run_shell", "run_write", "click",
                                   "type_text", "key", "run_app"],
            "policy": "company approval gate (default deny until granted)",
            "pending": [
                {"id": p[0], "action": p[1],
                 "since": p[2].isoformat() if p[2] else None}
                for p in pending_auth
            ],
        },
        "card_version": "1.0",
    }


@router.post("/{aid}/skills/provision", response_model=AgentSkillProvisionOut)
async def provision_agent_skill_endpoint(aid: str, deep: bool = True,
                                         db: AsyncSession = Depends(get_db),
                                         user=Depends(get_current_user)):
    """给已有员工补齐技能：本地库 → 公开市场（deep=true）→ 生成。

    创建员工时已自动配备一次；这个接口用于「后来发现技能不够」时手动补齐。
    外呼与生成都有上限，失败逐级降级，不会让员工不可用。
    """
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    from app.skill_provisioning import provision_agent_skills
    result = await provision_agent_skills(
        db, a, hints=[a.role or "", a.description or "", a.system_prompt or ""],
        allow_market=deep, allow_generate=True)
    await db.commit()
    return result


@router.patch("/{aid}", response_model=AgentOut)
async def update_agent(aid: str, data: dict, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    a = await _load_agent(db, aid)
    # 启停是不可逆的"让人下线"动作：MANAGE 起（member 一律 404），
    # 其余配置改动按 OPERATE 走写范围（自己的 / 本部门的）。
    if data.get("status"):
        await ensure_agent_access(db, user, a, Capability.MANAGE)
    else:
        await ensure_agent_access(db, user, a, Capability.OPERATE)
    # 可见性也是一项"广播权"：与创建同口径，只有 owner/admin 能给 company
    if "visibility" in data:
        visibility = _normalize_visibility(data.get("visibility"))
        if visibility == VISIBILITY_COMPANY:
            ctx = await build_ctx(db, user, a.company_id)
            if not ctx.sees_all_agents:
                raise HTTPException(404, "Not found")
        a.visibility = visibility
    if "department_id" in data and data.get("department_id"):
        ctx = await build_ctx(db, user, a.company_id)
        await ensure_dept_access(ctx, data["department_id"], db=db)
    # 能力包 / 底座更新
    if "capabilities" in data or "backend" in data:
        _apply_capabilities(a, data.get("capabilities") or [],
                            data.get("backend"))
    allowed = {"name", "avatar", "role", "description", "system_prompt",
               "department_id", "manager_agent_id", "status"}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k == "status":
            if v is None:
                continue
            try:
                a.status = AgentStatus(v)
            except ValueError:
                raise HTTPException(422, f"非法状态: {v}")
        else:
            setattr(a, k, v)
    await db.commit()
    await db.refresh(a)
    return a


@router.delete("/{aid}", status_code=204)
async def delete_agent(aid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    a = await _load_agent(db, aid)
    # 删除员工不可逆：其技能绑定、记忆、档案一并消失。
    # DESTRUCTIVE 不在 member 矩阵里，部门负责人也删不了——删除权不下放。
    await ensure_agent_access(db, user, a, Capability.DESTRUCTIVE)
    # 技能绑定没有数据库级级联：不先清就删人会触发 FK 失败
    # （自动配备技能上线后，这一点几乎每个员工都会命中）。
    from app.models.skill import AgentSkill
    await db.execute(delete(AgentSkill).where(AgentSkill.agent_id == a.id))
    await db.delete(a)
    await db.commit()


class BatchDeleteIn(BaseModel):
    agent_ids: list[str]


@router.post("/batch-delete", response_model=AgentBatchDeleteOut)
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
    # 预校验权限：按"公司 × 写范围"一次性查出可删 id，循环内不再解析角色（防 N+1）。
    # 仍用 SQL 条件而不是拉全量再过滤；DESTRUCTIVE 不在 member 矩阵里，删除权不下放。
    deletable_ids: set[str] = set()
    for cid in await _user_company_ids(db, user):
        ctx = await build_ctx(db, user, cid)
        if not role_has(ctx.role, Capability.DESTRUCTIVE):
            continue
        rows = await db.execute(select(Agent.id).where(agent_write_scope(ctx)))
        deletable_ids.update(rows.scalars().all())

    deleted, failed = [], []
    for aid in data.agent_ids:
        if aid not in deletable_ids:
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
                    "DELETE FROM execution_checkpoints WHERE execution_id IN "
                    "(SELECT id FROM agent_executions WHERE agent_id = :aid)"),
                    {"aid": aid})
                await db.execute(text(
                    "DELETE FROM agent_executions WHERE agent_id = :aid"), {"aid": aid})
                # 3) 删除本体（其余关联表为 CASCADE 自动清理）
                await db.delete(a)
                # 显式 flush：单条失败在 savepoint 内暴露，不影响其他条目
                await db.flush()
            deleted.append(aid)
        except (IntegrityError, DBAPIError) as e:
            # savepoint 已由 begin_nested 上下文回滚；此处不能再 rollback 外层事务
            failed.append({"id": aid, "reason": f"数据库约束: {str(getattr(e, 'orig', e))[:120]}"})
            db.expire_all()  # 回滚后 ORM 对象已过期，避免后续触发同步 IO
        except HTTPException:
            failed.append({"id": aid, "reason": "无权限"})
    if deleted:
        await log_event(db, "agent.batch_deleted", "agent", None, "user", user_id,
                        {"count": len(deleted)})
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        failed.extend({"id": aid, "reason": f"提交失败: {str(e)[:80]}"} for aid in deleted)
        deleted = []
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

    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    r = await self_reflect(db, a)
    return r


@router.post("/batch-create", status_code=201,
             response_model=AgentBatchCreateOut)
async def batch_create_agents(data: BatchCreateIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """批量创建 Agent，可统一指定部门/上级/模型。"""
    c = await db.get(Company, data.company_id)
    if not c:
        raise HTTPException(404, "Company not found")
    await ensure_capability(db, user.id, data.company_id, Capability.OPERATE)
    ctx = await build_ctx(db, user, data.company_id)

    # 配额：按目标部门聚合（批量接口不能成为绕过配额的后门）。
    # 先校验再落行，避免"建了一半才 409"留下半成品。
    buckets: dict[str | None, int] = {}
    for item in data.agents:
        if not (item.get("name") or "").strip():
            continue
        dept_id = item.get("department_id") or data.department_id
        buckets[dept_id] = buckets.get(dept_id, 0) + 1
    for dept_id, adding in buckets.items():
        if dept_id:
            await ensure_dept_access(ctx, dept_id, db=db)
        await _enforce_agent_quota(db, c, user, dept_id, adding=adding,
                                   label=f"{adding} 个 Agent")

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
            # 与单个创建同口径：写归属 + 默认部门可见，否则创建者自己都看不到
            created_by_user_id=user.id,
            visibility=VISIBILITY_DEPARTMENT,
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

    # 批量创建只做本地库匹配 + 通用兜底：避免 N 个员工各触发一次模型生成。
    try:
        from app.skill_provisioning import provision_agent_skills
        for agent, item in created:
            await provision_agent_skills(
                db, agent,
                hints=[str(item.get("role") or ""),
                       str(item.get("description") or "")],
                allow_market=False, allow_generate=False)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "批量创建 Agent 的技能配备失败: %s: %s", type(exc).__name__, exc)

    return {
        "created": [{"id": a.id, "name": a.name, "role": a.role} for a, _ in created],
        "created_count": len(created),
    }


class SetManagerIn(BaseModel):
    manager_agent_id: str | None  # None 表示清除上级


@router.put("/{aid}/manager", response_model=AgentManagerOut)
async def set_manager(aid: str, data: SetManagerIn,
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """设置/更换 Agent 的上级（组织架构汇报关系）。"""
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.OPERATE)

    if data.manager_agent_id:
        if data.manager_agent_id == aid:
            raise HTTPException(400, "不能把自己设为上级")
        mgr = await _load_agent(db, data.manager_agent_id)
        # 上级也必须在自己的写范围内，否则等于借机把别人的员工挂到自己名下
        await ensure_agent_access(db, user, mgr, Capability.OPERATE)
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


@router.get("/{aid}/subordinates", response_model=list[AgentSubordinateOut])
async def list_subordinates(aid: str, db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    """获取某 Agent 的全部直接下属。"""
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.VIEW)
    q = await db.execute(select(Agent).where(Agent.manager_agent_id == aid))
    return [{"id": s.id, "name": s.name, "role": s.role, "status": s.status.value
             if hasattr(s.status, "value") else str(s.status)} for s in q.scalars()]


class BatchAssignManagerIn(BaseModel):
    assignments: list[dict]  # [{agent_id, manager_agent_id|null}]


@router.post("/batch-assign-manager",
             response_model=AgentBatchAssignManagerOut)
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
            await ensure_agent_access(db, user, a, Capability.OPERATE)
            if mid:
                if mid == aid:
                    raise ValueError("不能把自己设为上级")
                mgr = await db.get(Agent, mid)
                if not mgr:
                    raise ValueError("上级不存在")
                await ensure_agent_access(db, user, mgr, Capability.OPERATE)
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


async def _require_chat_model(db: AsyncSession, model_id: str):
    """员工绑定的模型必须能对话。

    图片/视频/音频/向量模型走的是别的接口，绑成员工的对话模型后每一轮都
    是上游 400，而报错往往只有一句 Bad Request，看不出是绑错了类型。
    """
    from app.model_discovery import chat_model_rejection, is_chat_model
    from app.models import Model

    m = await db.get(Model, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    if not is_chat_model(m.model_type):
        raise HTTPException(400, chat_model_rejection(m))
    return m


@router.post("/{aid}/models", status_code=201,
             response_model=AgentModelConfigOut)
async def bind_model(aid: str, data: ModelBindingIn,
                     db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    await _require_chat_model(db, data.model_id)
    binding = AgentModelConfig(agent_id=aid, role=data.role, model_id=data.model_id)
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return binding


class SwitchModelIn(BaseModel):
    model_id: str  # 新的模型 ID


@router.put("/{aid}/switch-model", status_code=200,
            response_model=AgentSwitchModelOut)
async def switch_model(aid: str, data: SwitchModelIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """切换 Agent 的主模型（替换 existing primary binding）。"""
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    # 先校验再删旧绑定：否则一个被拒的切换请求会把员工原有的模型清掉
    await _require_chat_model(db, data.model_id)

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


@router.get("/{aid}/models", response_model=list[AgentModelBindingOut])
async def list_models_for_agent(aid: str, db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.VIEW)
    q = await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == aid))
    return [{"role": b.role, "model_id": b.model_id} for b in q.scalars()]


@router.get("/{aid}/skills", response_model=list[AgentSkillItemOut])
async def list_skills_for_agent(aid: str, db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    """员工当前绑定的技能（含绑定来源）。

    CEO 对话框需要知道"哪些技能已经勾上"；绑定/解绑仍走
    ``/skills/{slug}/install|uninstall``，不另造写接口。
    """
    from app.models.skill import AgentSkill, Skill as SkillRow

    a = await _load_agent(db, aid)
    await ensure_agent_access(db, user, a, Capability.VIEW)
    rows = (await db.execute(
        select(SkillRow.id, SkillRow.slug, SkillRow.name, SkillRow.description,
               SkillRow.scope, SkillRow.source, AgentSkill.source)
        .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
        .where(AgentSkill.agent_id == str(a.id))
        .order_by(SkillRow.name))).all()
    return [
        {"id": sid, "slug": slug, "name": name, "description": desc,
         "scope": scope, "source": source,
         "binding_source": binding_source or "manual"}
        for sid, slug, name, desc, scope, source, binding_source in rows
    ]
