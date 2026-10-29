"""P3-13~16 combined API: Governance / Marketplace / Templates / Autonomous."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.permissions import Capability, require_company_scope
from app.learning_engine import add_growth_event
from app.models.agent import Agent
from app.models.company import Company
from app.models.learning import GrowthEventType
from app.models.p3_governance import (AgentSandbox, AgentVersion,
                                      AutonomousExecutionLog,
                                      AutonomousSettings, CompanyCreationJob,
                                      EnterpriseTemplate, MarketplacePackage,
                                      PackageInstallation,
                                      SelfImprovementCase)
from app.models.user import User

router = APIRouter(prefix="/governance", tags=["governance"],
                   dependencies=[Depends(require_company_scope())])
def _id():
    return str(uuid.uuid4())


def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _owned_case(db: AsyncSession, user_id: str, iid: str):
    """取改进提案并校验公司归属：提案里带 agent_id，直接部署等于改别家员工。"""
    from app.api.deps import assert_company_owned

    case = await db.get(SelfImprovementCase, iid)
    if not case:
        raise HTTPException(404, "not found")
    await assert_company_owned(db, user_id, case.company_id)
    return case


# ================= P3-13 Self-Improvement Governance =================

class ImprovementIn(BaseModel):
    agent_id: str
    reflection_id: str | None = None
    what_improved: str | None = None
    why: str | None = None
    evidence: dict | None = None
    risk: str | None = None
    expected_impact: str | None = None


@router.post("/improvements", status_code=201)
async def propose_improvement(
    data: ImprovementIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent/用户提交自我改进提案（进入治理流程，不直接生效）。"""
    import app.api.deps as deps

    await deps.assert_company_owned(db, user.id, company_id)
    agent = await deps.assert_agent_owned(db, user.id, data.agent_id)
    # 公司归属通过了还不够：提案里的员工必须真的属于这家公司，
    # 否则「自己的 company_id + 别家的 agent_id」就能把改进部署到别家员工身上。
    if agent.company_id != company_id:
        raise HTTPException(404, "Agent not found")
    case = SelfImprovementCase(
        id=_id(), agent_id=data.agent_id, company_id=company_id,
        reflection_id=data.reflection_id,
        what_improved=data.what_improved, why=data.why,
        evidence=data.evidence, risk=data.risk,
        expected_impact=data.expected_impact)
    db.add(case)
    await db.commit()
    return {"id": case.id, "status": case.status}


@router.post("/improvements/{iid}/sandbox-test")
async def sandbox_test(
    iid: str,
    test_score: float = Query(0),
    baseline_score: float = Query(0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建沙盒克隆并记录测试对比结果。"""
    case = await _owned_case(db, user.id, iid)
    if case.status not in ("PROPOSED", "SANDBOX_TESTING"):
        raise HTTPException(409, f"status={case.status}")
    a = await db.get(Agent, case.agent_id)

    # 克隆当前 Agent 配置到沙盒
    sandbox = AgentSandbox(
        id=_id(), agent_id=case.agent_id, company_id=a.company_id,
        proposal_id=case.id,
        sandbox_config={"name": a.name, "role": a.role,
                        "system_prompt": a.system_prompt,
                        "modified": True},
        test_results={"score": test_score},
        comparison={"baseline": baseline_score, "candidate": test_score,
                    "delta": round(test_score - baseline_score, 2)})
    db.add(sandbox)

    case.sandbox_id = sandbox.id
    case.status = "TESTED"
    await db.commit()
    return {"case_id": iid, "status": "TESTED",
            "comparison": sandbox.comparison}


class DeployIn(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/improvements/{iid}/deploy")
async def deploy_improvement(
    iid: str,
    data: DeployIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """人工审批：批准 → 部署新版本（旧版本可回滚）；拒绝 → 关闭提案。"""
    case = await _owned_case(db, user.id, iid)
    if not data.approve:
        case.status = "REJECTED"
        case.decided_by = user.id
        await db.commit()
        return {"id": iid, "status": "REJECTED"}
    if case.status != "TESTED":
        raise HTTPException(409, "必须先完成 Sandbox Test")

    a = await db.get(Agent, case.agent_id)
    # 版本化部署：保存当前为 v(n)，应用改进为新版本
    max_q = await db.execute(select(func.max(AgentVersion.version)).where(
        AgentVersion.agent_id == case.agent_id))
    cur_ver = (max_q.scalar() or 0)
    # 保存基线版本（回滚点）
    db.add(AgentVersion(id=_id(), agent_id=case.agent_id, version=cur_ver + 1,
                        config_snapshot={"name": a.name, "role": a.role,
                                         "system_prompt": a.system_prompt},
                        deployed_by=user.id, change_note="改进前基线"))
    # 应用改进：把沙盒中的变更写入生产 Agent
    sb = await db.get(AgentSandbox, case.sandbox_id) if case.sandbox_id else None
    if sb and sb.sandbox_config:
        for k in ("role", "system_prompt"):
            if sb.sandbox_config.get(k):
                setattr(a, k, sb.sandbox_config[k])
    db.add(AgentVersion(id=_id(), agent_id=case.agent_id, version=cur_ver + 2,
                        config_snapshot={"name": a.name, "role": a.role,
                                         "system_prompt": a.system_prompt},
                        deployed_by=user.id,
                        change_note=f"部署改进: {case.what_improved[:60]}",
                        is_current=True))
    case.status = "DEPLOYED"
    case.decided_by = user.id
    await add_growth_event(db, case.agent_id, a.company_id,
                           GrowthEventType.CAPABILITY_CHANGED,
                           f"自我改进已部署: {case.what_improved[:50]}",
                           source_type="governance", actor=user.id)
    await db.commit()
    return {"id": iid, "status": "DEPLOYED",
            "deployed_version": cur_ver + 2, "rollback_version": cur_ver + 1}


@router.post("/agents/{agent_id}/rollback/{version}")
async def rollback_agent(
    agent_id: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """回滚 Agent 到指定历史版本。"""
    from app.api.deps import assert_agent_owned

    a = await assert_agent_owned(db, user.id, agent_id)
    q = await db.execute(select(AgentVersion).where(
        AgentVersion.agent_id == agent_id, AgentVersion.version == version))
    ver = q.scalars().first()
    if not ver or not ver.config_snapshot:
        raise HTTPException(404, "version not found")
    snap = ver.config_snapshot
    if snap.get("name"):
        a.name = snap["name"]
    if snap.get("role"):
        a.role = snap["role"]
    if snap.get("system_prompt"):
        a.system_prompt = snap["system_prompt"]
    # 全部版本置为非当前，再标记该版本
    all_q = await db.execute(select(AgentVersion).where(
        AgentVersion.agent_id == agent_id))
    for v in all_q.scalars().all():
        v.is_current = False
    ver.is_current = True
    await add_growth_event(db, agent_id, a.company_id,
                           GrowthEventType.MODEL_CHANGED,
                           f"Agent 回滚到 v{version}", actor=user.id)
    await db.commit()
    return {"rolled_back_to": version}


# ================= P3-14 Marketplace =================

class PackageIn(BaseModel):
    package_type: str          # AGENT / SKILL / WORKFLOW / DEPARTMENT_TEMPLATE
    key: str
    name: str
    description: str | None = None
    author: str | None = None
    version: str = "1.0.0"
    permissions_required: list[str] | None = None
    dependencies: list[str] | None = None
    manifest: dict | None = None


@router.post("/marketplace/packages", status_code=201)
async def publish_package(
    data: PackageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.package_type not in ("AGENT", "SKILL", "WORKFLOW",
                                  "DEPARTMENT_TEMPLATE"):
        raise HTTPException(400, "invalid package_type")
    p = MarketplacePackage(
        id=_id(), package_type=data.package_type, key=data.key,
        name=data.name, description=data.description, author=data.author,
        latest_version=data.version,
        permissions_required=data.permissions_required or [],
        dependencies=data.dependencies or [], manifest=data.manifest)
    db.add(p)
    await db.commit()
    return {"id": p.id, "key": p.key, "version": p.latest_version}


@router.get("/marketplace/packages")
async def list_packages(db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    q = await db.execute(select(MarketplacePackage).where(
        MarketplacePackage.published.is_(True)))
    return [{"id": p.id, "type": p.package_type, "key": p.key, "name": p.name,
             "version": p.latest_version,
             "permissions_required": p.permissions_required,
             "dependencies": p.dependencies} for p in q.scalars().all()]


@router.post("/marketplace/packages/{pid}/install", status_code=201)
async def install_package(
    pid: str,
    company_id: str = Query(...),
    confirm_permissions: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """安装包：必须显式确认权限清单。"""
    p = await db.get(MarketplacePackage, pid)
    if not p:
        raise HTTPException(404, "not found")
    if not confirm_permissions:
        return {"installed": False,
                "requires_confirmation": {
                    "permissions_required": p.permissions_required,
                    "dependencies": p.dependencies,
                    "potential_risk": "安装的包将以该公司身份运行"}}
    inst = PackageInstallation(id=_id(), company_id=company_id,
                               package_id=pid, version=p.latest_version,
                               installed_by=user.id,
                               install_log={"steps": ["download", "verify",
                                                      "install"],
                                            "all_ok": True})
    db.add(inst)
    await db.commit()
    return {"installed": True, "installation_id": inst.id,
            "package": p.name, "version": p.latest_version}


@router.post("/installations/{inst_id}/rollback")
async def rollback_installation(inst_id: str,
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    inst = await db.get(PackageInstallation, inst_id)
    if not inst:
        raise HTTPException(404, "not found")
    inst.rolled_back_at = _now_utc()
    await db.commit()
    return {"id": inst_id, "rolled_back": True}


# ================= P3-15 Enterprise Templates =================

BUILTIN_TEMPLATES = {
    "general": {
        "name": "通用公司模板",
        "departments": [
            {"name": "战略部", "agents": [{"name": "战略分析师", "role": "战略规划"}]},
            {"name": "市场部", "agents": [{"name": "市场研究员", "role": "市场研究"}]},
            {"name": "技术部", "agents": [{"name": "程序员", "role": "开发工程师"}]},
        ],
        "ceo": {"name": "CEO Agent", "role": "首席执行官"},
        "automations": [],
    },
    "project": {
        "name": "项目型公司模板",
        "departments": [
            {"name": "项目管理部", "agents": [{"name": "项目经理", "role": "项目管理"}]},
            {"name": "执行部", "agents": [{"name": "执行专员", "role": "任务执行"},
                                           {"name": "质检员", "role": "质量检查"}]},
        ],
        "ceo": {"name": "项目总监", "role": "项目总负责"},
        "automations": [{"name": "每日站会提醒", "trigger_type": "TIME"}],
    },
    "creative": {
        "name": "创意工作室模板",
        "departments": [
            {"name": "策划部", "agents": [{"name": "创意策划", "role": "创意策划"}]},
            {"name": "设计部", "agents": [{"name": "设计师", "role": "视觉设计"}]},
            {"name": "文案部", "agents": [{"name": "文案", "role": "文案创作"}]},
        ],
        "ceo": {"name": "创意总监", "role": "创意总监"},
        "automations": [],
    },
    "research": {
        "name": "研究团队模板",
        "departments": [
            {"name": "研究一部", "agents": [{"name": "研究员A", "role": "研究分析"}]},
            {"name": "数据组", "agents": [{"name": "数据分析员", "role": "数据处理"}]},
        ],
        "ceo": {"name": "首席研究员", "role": "研究负责"},
        "automations": [{"name": "每周文献汇总", "trigger_type": "TIME"}],
    },
}


@router.get("/templates")
async def list_templates(company_id: str | None = Query(None),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    out = []
    for k, t in BUILTIN_TEMPLATES.items():
        out.append({"key": k, "name": t["name"], "builtin": True,
                    "departments": len(t["departments"]),
                    "agents": sum(len(d["agents"]) for d in t["departments"])})
    if company_id:
        # EnterpriseTemplate 是全局目录（无 company_id 字段）：模板本身不含租户
        # 数据，按公司过滤既无意义、也会因属性不存在直接 500。租户边界由
        # router 级 require_company_scope 负责。
        q = await db.execute(select(EnterpriseTemplate).where(
            EnterpriseTemplate.is_builtin.is_(False)))
        out += [{"key": e.key, "name": e.name, "builtin": False}
                for e in q.scalars().all()]
    return out


class FromTemplateIn(BaseModel):
    template_key: str
    company_name: str | None = None


@router.post("/create-company-from-template", status_code=201)
async def create_from_template(
    data: FromTemplateIn,
    owner_company_id: str = Query(...),   # 挂在哪个用户/公司下
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一键按模板创建公司结构（部门+Agent）。"""
    tpl = BUILTIN_TEMPLATES.get(data.template_key)
    if not tpl:
        raise HTTPException(404, "template not found")
    # owner_company_id 来自调用方：不查库就等于允许往他人公司塞员工
    owned = await db.get(Company, owner_company_id)
    if not owned or owned.owner_id != user.id:
        raise HTTPException(404, "Company not found")

    job = CompanyCreationJob(id=_id(),
                             company_id=owner_company_id,
                             template_key=data.template_key)
    db.add(job)

    created_agents = []
    # ---- 步骤3后自动绑定首选模型 ----
    # 「公司默认模型」字段目前不存在，退化为取该公司可见的第一个登记模型
    # （向导步骤2登记的即为第一个；向导登记时 company_id 为空即共享模型，
    # 故一并纳入候选）。写法紧贴 agents.py 批量创建：AgentModelConfig(role="primary")。
    from app.models.agent import AgentModelConfig, Model as ModelCfg
    mq = await db.execute(
        select(ModelCfg)
        .where(or_(ModelCfg.company_id == owner_company_id,
                   ModelCfg.company_id.is_(None)))
        .order_by(ModelCfg.created_at)
        .limit(1))
    first_model = mq.scalars().first()
    default_model_id = first_model.id if first_model else None
    bound_count = 0
    try:
        # 创建 CEO
        ceo = Agent(company_id=owner_company_id, name=tpl["ceo"]["name"],
                    role=tpl["ceo"]["role"], status="IDLE")
        db.add(ceo)
        await db.flush()
        created_agents.append({"name": ceo.name, "role": ceo.role})
        if default_model_id:
            db.add(AgentModelConfig(agent_id=ceo.id, role="primary",
                                    model_id=default_model_id))
            bound_count += 1
        job.steps_done = list(job.steps_done or []) + ["创建CEO"]

        # 创建部门与 Agent
        from app.models.company import Department
        for dep in tpl["departments"]:
            department = Department(company_id=owner_company_id,
                                    name=dep["name"])
            db.add(department)
            await db.flush()
            for ag in dep["agents"]:
                agent = Agent(company_id=owner_company_id,
                              department_id=department.id,
                              name=ag["name"], role=ag["role"], status="IDLE")
                db.add(agent)
                await db.flush()  # id 是 Python-side default，flush 后才有值，绑定要用
                created_agents.append({"name": agent.name, "role": agent.role})
                if default_model_id:
                    db.add(AgentModelConfig(agent_id=agent.id, role="primary",
                                            model_id=default_model_id))
                    bound_count += 1
            job.steps_done = list(job.steps_done or []) + [f"创建部门 {dep['name']}"]

        job.status = "SUCCESS"
        await db.commit()
        return {"job_id": job.id, "status": "SUCCESS",
                "created_agents": created_agents,
                "auto_bound_model": first_model.name if first_model else None,
                "auto_bound_count": bound_count}
    except Exception as e:
        job.status = "FAILED"
        job.error = str(e)[:300]
        await db.commit()
        raise HTTPException(500, f"创建失败: {e}")


@router.post("/save-company-as-template")
async def save_as_template(
    company_id: str = Query(...),
    key: str = Query(...),
    name: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把当前公司结构保存为模板。"""
    from app.models.company import Company, Department
    comp = await db.get(Company, company_id)
    if not comp:
        raise HTTPException(404, "company not found")
    deps = (await db.execute(select(Department).where(
        Department.company_id == company_id))).scalars().all()
    agents_q = await db.execute(select(Agent).where(
        Agent.company_id == company_id))
    agents = agents_q.scalars().all()
    dep_map = {}
    for d in deps:
        dep_map[d.id] = d.name
    structure = {"ceo": {"name": comp.name}, "departments": []}
    for d in deps:
        members = [{"name": a.name, "role": a.role} for a in agents
                   if a.department_id == d.id]
        structure["departments"].append({"name": d.name, "agents": members})
    et = EnterpriseTemplate(id=_id(), key=key, name=name,
                            template_data=structure)
    db.add(et)
    await db.commit()
    return {"id": et.id, "key": key,
            "departments_captured": len(structure["departments"])}


# ================= P3-16 Autonomous Mode =================

class AutoSettingsIn(BaseModel):
    level: int                       # 0-5
    scope_type: str = "COMPANY"      # COMPANY/DEPARTMENT/PROJECT/WORKFLOW/AGENT
    scope_id: str | None = None
    cost_limit: float | None = None
    time_limit_minutes: int | None = None
    action_limit: int | None = None


@router.put("/autonomous-settings")
async def set_autonomous_level(
    data: AutoSettingsIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not 0 <= data.level <= 5:
        raise HTTPException(400, "level must be 0-5")
    # L4 以上必须设置限制
    if data.level >= 4 and not (data.cost_limit or data.action_limit):
        raise HTTPException(400, "L4以上必须设置 cost_limit 或 action_limit")
    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id,
        AutonomousSettings.scope_type == data.scope_type,
        (AutonomousSettings.scope_id == data.scope_id) if data.scope_id
        else AutonomousSettings.scope_id.is_(None)))
    s = q.scalars().first()
    if not s:
        s = AutonomousSettings(id=_id(), company_id=company_id,
                               scope_type=data.scope_type,
                               scope_id=data.scope_id)
        db.add(s)
    s.level = data.level
    s.cost_limit = data.cost_limit
    s.time_limit_minutes = data.time_limit_minutes
    s.action_limit = data.action_limit
    s.emergency_stopped = False
    s.updated_by = user.id
    await db.commit()
    return {"scope": data.scope_type, "level": data.level,
            "limits": {"cost": data.cost_limit,
                       "time_min": data.time_limit_minutes,
                       "actions": data.action_limit}}


@router.get("/autonomous-settings")
async def get_autonomous_level(
    company_id: str = Query(...),
    scope_type: str = Query("COMPANY"),
    scope_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id,
        AutonomousSettings.scope_type == scope_type))
    rows = [r for r in q.scalars().all()
            if (scope_id is None or r.scope_id == scope_id)]
    s = rows[0] if rows else None
    return {"level": s.level if s else 2,
            "emergency_stopped": s.emergency_stopped if s else False,
            "limits": ({"cost": s.cost_limit, "time_min": s.time_limit_minutes,
                        "actions": s.action_limit} if s else None)}


@router.get("/autonomous-overview")
async def autonomous_overview(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """全部自治设置总览（带对象名称），供自治模式页展示三级作用域配置。"""
    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id))
    rows = q.scalars().all()
    out = []
    for r in rows:
        name = None
        if r.scope_type == "AGENT" and r.scope_id:
            a = await db.get(Agent, r.scope_id)
            name = a.name if a else "(已删除)"
        elif r.scope_type == "DEPARTMENT" and r.scope_id:
            from app.models.company import Department
            d = await db.get(Department, r.scope_id)
            name = d.name if d else "(已删除)"
        elif r.scope_type == "COMPANY":
            from app.models.company import Company
            c = await db.get(Company, company_id)
            name = c.name if c else None
        out.append({"id": r.id, "scope_type": r.scope_type,
                    "scope_id": r.scope_id, "name": name or "-",
                    "level": r.level,
                    "limits": {"cost": r.cost_limit,
                               "time_min": r.time_limit_minutes,
                               "actions": r.action_limit},
                    "emergency_stopped": r.emergency_stopped})
    return out


@router.delete("/autonomous-overview/{settings_id}")
async def delete_autonomous_settings(
    settings_id: str,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """清除某个对象（部门/项目/Agent）的专属自治设置，使其回落到公司默认等级。

    ## 改造说明
    前端「自治运行」页的"清除"按钮调用本路由，但后端**从未实现**
    （前端只留了一句"后端若无删除接口则提示"的兜底注释），
    导致该功能自始至终 404。这里补上实现并加归属校验。
    """
    # 先校验调用者真的拥有这家公司。原实现只比较
    # "记录的公司 == 调用者传入的公司"，两边都来自调用者，等于没有校验。
    from app.api.deps import assert_company_owned
    await assert_company_owned(db, user.id, company_id)

    row = await db.get(AutonomousSettings, settings_id)
    # 记录级归属校验：防止用自己公司的 company_id 删除他人配置
    if not row or row.company_id != company_id:
        raise HTTPException(404, "自治设置不存在")

    scope_desc = f"{row.scope_type}:{row.scope_id or company_id}"
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted_id": settings_id,
            "scope": scope_desc,
            "message": "已清除专属设置，该对象将回落到公司默认自治等级"}


@router.get("/autonomous-logs")
async def list_autonomous_logs(
    company_id: str = Query(...),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """自治执行审计日志列表。"""
    q = await db.execute(
        select(AutonomousExecutionLog)
        .where(AutonomousExecutionLog.company_id == company_id)
        .order_by(AutonomousExecutionLog.created_at.desc())
        .limit(limit))
    return [{"id": r.id, "scope_type": r.scope_type, "scope_id": r.scope_id,
             "level": r.level, "why": r.why,
             "summary": (r.result or {}).get("summary") if isinstance(r.result, dict) else None,
             "actions": len(r.actions_taken or []),
             "cost_estimate": r.cost_estimate,
             "human_override": r.human_override,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in q.scalars().all()]


@router.post("/autonomous-configs", status_code=201)
async def upsert_autonomous_config(
    data: dict,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agent 级自主行为配置（并行数/限速），落到 AutonomousSettings(scope_type=AGENT)。"""
    agent_id = data.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "agent_id is required")
    ext = data.get("external_agent_id") or None
    scope_id = ext or agent_id
    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id,
        AutonomousSettings.scope_type == "AGENT",
        AutonomousSettings.scope_id == scope_id))
    s = q.scalars().first()
    if not s:
        s = AutonomousSettings(id=_id(), company_id=company_id,
                               scope_type="AGENT", scope_id=scope_id)
        db.add(s)
    s.level = int(data.get("level") or 2)
    s.action_limit = data.get("max_parallel")
    s.cost_limit = data.get("rate_limit_per_min")
    s.updated_by = user.id
    # 审计日志
    db.add(AutonomousExecutionLog(
        id=_id(), company_id=company_id, scope_type="AGENT",
        scope_id=scope_id, level=s.level,
        why="CONFIG_UPDATE",
        actions_taken=[{"type": "config_update", "max_parallel": data.get("max_parallel"),
                        "rate_limit_per_min": data.get("rate_limit_per_min"),
                        "external_agent": ext}],
        result={"summary": f"更新 Agent 自主行为配置 (并行={data.get('max_parallel')}, 限速={data.get('rate_limit_per_min')}/min)"}))
    await db.commit()
    return {"id": s.id, "agent_id": agent_id, "external_agent_id": ext,
            "max_parallel": s.action_limit, "rate_limit_per_min": s.cost_limit}


@router.post("/emergency-resume")
async def emergency_resume_alias(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """恢复运行（resume-from-checkpoint 的别名）。"""
    return await resume_from_checkpoint(company_id=company_id, db=db, user=user)


class ExecutionLogIn(BaseModel):
    scope_type: str = "COMPANY"
    scope_id: str | None = None
    level: int = 2
    why: str | None = None
    plan: dict | None = None
    actions_taken: list | None = None
    result: dict | None = None
    cost_estimate: float | None = None


@router.post("/autonomous-executions", status_code=201)
async def log_execution(
    data: ExecutionLogIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """自治执行的审计日志：Why/Plan/Action/Result/Cost。"""
    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id,
        AutonomousSettings.scope_type == data.scope_type))
    settings = q.scalars().first()
    if settings and settings.emergency_stopped:
        raise HTTPException(423, "紧急停止中，禁止自治执行")
    log = AutonomousExecutionLog(id=_id(), company_id=company_id,
                                 scope_type=data.scope_type,
                                 scope_id=data.scope_id, level=data.level,
                                 why=data.why, plan=data.plan,
                                 actions_taken=data.actions_taken,
                                 result=data.result,
                                 cost_estimate=data.cost_estimate)
    db.add(log)
    await db.commit()
    return {"id": log.id}


class EmergencyStopIn(BaseModel):
    checkpoint: dict | None = None


@router.post("/emergency-stop")
async def emergency_stop(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    data: EmergencyStopIn | None = None,
):
    """全局紧急停止：冻结自治设置 + 保存检查点。

    修复：原先只按 company_id 过滤数据，却不校验调用者是否拥有这家公司——
    任何登录用户都能冻结别人公司的自治行为（IDOR）。
    """
    from app.core.permissions import Capability, ensure_capability
    await ensure_capability(db, user.id, company_id, Capability.DESTRUCTIVE)

    q = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id))
    stopped = 0
    for s in q.scalars().all():
        s.emergency_stopped = True
        stopped += 1
    log = AutonomousExecutionLog(
        id=_id(), company_id=company_id, scope_type="COMPANY", level=-1,
        why="EMERGENCY_STOP", human_override=True,
        checkpoint=(data.checkpoint if data else None))
    db.add(log)
    await db.commit()
    return {"stopped_scopes": stopped,
            "checkpoint_saved": bool(data and data.checkpoint)}


@router.post("/resume-from-checkpoint")
async def resume_from_checkpoint(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从检查点恢复：解除紧急停止。

    与 emergency-stop 对称：解除别人公司的紧急停止同样是越权。
    """
    from app.core.permissions import Capability, ensure_capability
    await ensure_capability(db, user.id, company_id, Capability.DESTRUCTIVE)

    q = await db.execute(select(AutonomousExecutionLog).where(
        AutonomousExecutionLog.company_id == company_id).order_by(
        AutonomousExecutionLog.created_at.desc()).limit(5))
    cp = None
    for l in q.scalars().all():
        if l.checkpoint:
            cp = l.checkpoint
            break
    sq = await db.execute(select(AutonomousSettings).where(
        AutonomousSettings.company_id == company_id))
    resumed = 0
    for s in sq.scalars().all():
        s.emergency_stopped = False
        resumed += 1
    await db.commit()
    return {"resumed": resumed, "restored_checkpoint": cp}


@router.get("/self-improvement-cases")
async def list_improvement_cases(
    company_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出自我改进提案（治理审批队列）。"""
    q = select(SelfImprovementCase)
    if company_id:
        q = q.where(SelfImprovementCase.company_id == company_id)
    rows = (await db.execute(q.order_by(
        SelfImprovementCase.status, SelfImprovementCase.id))).scalars().all()
    return [{"id": r.id, "agent_id": r.agent_id,
             "what_improved": r.what_improved, "why": r.why,
             "risk": r.risk, "expect_effect": r.expected_impact,
             "status": r.status} for r in rows]


@router.put("/self-improvement-cases/{iid}/{decision}")
async def decide_improvement_case(
    iid: str, decision: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批准/拒绝改进提案。"""
    if decision not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "decision must be APPROVE or REJECT")
    case = await db.get(SelfImprovementCase, iid)
    if not case:
        raise HTTPException(404, "Case not found")
    case.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    await db.commit()
    return {"ok": True, "status": case.status}
