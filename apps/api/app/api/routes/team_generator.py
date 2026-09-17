"""一键生成全新 Agent 智能体团队公司。

流程：老板填写范本（行业/组织架构/需要哪些Agent/各自能力）→
LLM 解析为结构化蓝图 → 一键创建 公司+部门+Agent（含上下级关系、模型绑定）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent, AgentStatus
from app.models.company import Company, Department
from app.models.user import User
from app.model_gateway import ModelGateway

router = APIRouter(prefix="/team-generator", tags=["team-generator"])


class GenerateIn(BaseModel):
    company_name: str | None = None       # 不填则由 LLM 起名
    industry: str = ""                     # 行业
    blueprint: str                         # 老板填写的范本内容（自由文本）
    model_id: str | None = None            # 新 Agent 绑定的模型


async def _llm_plan(db: AsyncSession, system: str, user: str) -> dict:
    """用系统里第一个可用模型生成蓝图 JSON。"""
    from app.models import Model
    from app.model_gateway import ModelGateway
    row = (await db.execute(select(Model).limit(1))).scalar_one_or_none()
    if not row:
        raise HTTPException(500, "系统中未配置任何模型，无法解析范本")
    gw = ModelGateway(db)
    llm = await gw.chat_with_agent_models([str(row.id)], [
        {"role": "system", "content": system},
        {"role": "user", "content": user}])
    import json as _json, re as _re
    content = (llm.get("content") or "").strip()
    m = _re.search(r"\{[\s\S]*\}", content)
    if not m:
        raise HTTPException(502, f"模型未返回有效蓝图：{content[:200]}")
    try:
        plan = _json.loads(m.group(0))
    except Exception:
        raise HTTPException(502, f"蓝图解析失败：{m.group(0)[:200]}")
    return _normalize_plan(plan)


def _normalize_plan(plan: dict) -> dict:
    """把 LLM 各种形态的蓝图归一化为标准结构。"""
    import json as _json
    if not isinstance(plan, dict):
        raise ValueError("plan not dict")
    depts = []
    agents = []
    raw_depts = plan.get("departments") or []
    # 情况A：agents 正确在顶层
    raw_agents = plan.get("agents") or []

    def _agent_from(src, dept_hint=""):
        """从任意字段命名的 dict 提取标准 agent。"""
        name = src.get("name") or src.get("姓名") or src.get("agent_name")
        role = src.get("role") or src.get("岗位") or src.get("title") or src.get("responsibilities") or ""
        desc = src.get("description") or src.get("描述") or src.get("职责") or src.get("responsibilities") or role
        sp = (src.get("system_prompt") or src.get("prompt") or src.get("设定") or
              (f"你是{name or '员工'}，担任{role}。" +
               (f"职责：{desc}。" if desc else "") +
               (f"核心能力：{', '.join(src['capabilities'])}。" if isinstance(src.get("capabilities"), list) and src.get("capabilities") else "")))
        dept_name = src.get("department") or src.get("部门") or dept_hint or ""
        if not name:
            # 无姓名时用岗位作名字（如「主播」→「主播专员」），保证非0
            name = (role or f"成员")[:12]
        return {
            "name": name,
            "role": role if isinstance(role, str) else str(role),
            "department": dept_name,
            "description": desc if isinstance(desc, str) else str(desc),
            "is_leader": bool(src.get("is_leader") or src.get("负责人") or src.get("leader")),
            "reports_to": src.get("reports_to") or src.get("汇报给") or "",
            "system_prompt": sp if isinstance(sp, str) else str(sp),
        }

    for d in raw_depts:
        if isinstance(d, str):
            depts.append({"name": d, "description": ""})
            continue
        dname = d.get("name") or d.get("部门") or ""
        depts.append({"name": dname, "description": d.get("description") or d.get("职责") or ""})
        # 情况B：agents 被嵌套在部门里
        for sub in (d.get("agents") or d.get("members") or d.get("员工") or []):
            agents.append(_agent_from(sub if isinstance(sub, dict) else {"role": str(sub)}, dname))

    for a in raw_agents:
        if isinstance(a, str):
            agents.append(_agent_from({"role": a}))
        elif isinstance(a, dict):
            agents.append(_agent_from(a))

    # 兜底1：完全没有 agents 但有部门 → 每部门生成一名主管
    if not agents:
        for d in depts:
            agents.append(_agent_from(
                {"role": f"{d['name']}主管", "department": d["name"],
                 "description": f"{d['name']}负责人"}, d["name"]))
    # 兜底2：有 agents 但没部门 → 从范本常见关键词推断，否则建「综合部」
    known = {d["name"] for d in depts}
    if agents and not known:
        dept_names = sorted({a["department"] for a in agents if a.get("department")})
        if dept_names:
            for dn in dept_names:
                depts.append({"name": dn, "description": f"{dn}（依据成员岗位归纳）"})
                known.add(dn)
        else:
            depts.append({"name": "综合部", "description": "全体成员"})
            known.add("综合部")
        for a in agents:
            if not a.get("department"):
                a["department"] = "综合部"
    # 补漏：agent 的部门不在 departments 列表 → 自动补录
    for a in agents:
        if a.get("department") and a["department"] not in {d["name"] for d in depts}:
            depts.append({"name": a["department"], "description": ""})

    # 兜底3：全部为空 → 从范本原文提取，构造最小可行团队（负责人+专员）
    if not agents:
        # 从 plan 里找 LLM 可能放在其他 key 的内容
        raw_text = _json.dumps(plan, ensure_ascii=False) if plan else ""
        agents.append(_agent_from({
            "name": "总经理", "role": "总经理（负责人）",
            "department": "综合部", "description": "统筹公司整体运营与战略决策",
            "is_leader": True,
            "system_prompt": f"你是{plan.get('company_name') or '公司'}的总经理，全面统筹公司运营。"
                             f"业务背景：{(raw_text or '')[:300]}"}))
        agents.append(_agent_from({
            "name": "业务专员", "role": "业务专员",
            "department": "综合部", "description": "执行具体业务事项",
            "reports_to": "总经理",
            "system_prompt": "你是业务专员，负责落实总经理安排的各项任务，按时保质完成并汇报进展。"}))
    if not depts:
        depts.append({"name": "综合部", "description": "全体成员"})
    for a in agents:
        if not a.get("department"):
            a["department"] = "综合部"

    # 确保唯一 leader
    if not any(a["is_leader"] for a in agents) and agents:
        agents[0]["is_leader"] = True

    return {"company_name": plan.get("company_name"), "intro": plan.get("intro"),
            "departments": depts, "agents": agents}


@router.post("/generate")
async def generate_team(data: GenerateIn,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """范本 + 提示词 → LLM 生成团队蓝图 → 一键创建完整公司。"""
    plan = await _llm_plan(db,
        "你是企业组织架构设计专家。根据建团范本，输出一个 JSON 对象（只输出 JSON，禁止 markdown 代码块）。\n"
        "严格遵守以下结构，agents 必须是顶层独立数组，严禁把 agents 嵌套进 departments 里：\n"
        '{\n'
        ' "company_name": "公司名",\n'
        ' "intro": "一句话公司简介",\n'
        ' "departments": [{"name": "部门名", "description": "部门职责"}],\n'
        ' "agents": [\n'
        '   {"name": "员工姓名(如张伟)", "role": "岗位名称", "department": "所属部门名",\n'
        '    "description": "职责一句话", "is_leader": false, "reports_to": "",\n'
        '    "system_prompt": "详细人格与职责设定100-200字"}\n'
        ' ]\n'
        '}\n'
        "完整示例：\n"
        '{"company_name":"星火传媒","intro":"短视频营销公司",\n'
        ' "departments":[{"name":"内容部","description":"内容生产"},{"name":"运营部","description":"账号运营"}],\n'
        ' "agents":[\n'
        '  {"name":"王总","role":"总经理","department":"运营部","description":"统筹全局","is_leader":true,"reports_to":"","system_prompt":"你是星火传媒总经理王总…"},\n'
        '  {"name":"李编剧","role":"编导","department":"内容部","description":"脚本创作","is_leader":false,"reports_to":"王总","system_prompt":"你是编导李编剧…"}\n'
        ' ]}\n'
        "硬性要求：\n"
        "1. departments 与 agents 是两个并列的顶层数组；\n"
        "2. 每个 agent 必须有中文姓名(name)、岗位(role)、所属部门(department)；\n"
        "3. 有且只有一个 is_leader=true；其余 agent 的 reports_to 填上级姓名；\n"
        "4. 部门2-6个、Agent 3-12个；严格按范本，不擅自添加。",
        f"行业：{data.industry or '未指定'}\n"
        f"公司名：{data.company_name or '请你起一个贴切的名字'}\n"
        f"建团范本：\n{data.blueprint}")

    # ---- 创建公司 ----
    import uuid as _uuid
    company = Company(id=_uuid.uuid4().hex, name=plan.get("company_name") or data.company_name or "新公司",
                      owner_id=user.id,
                      description=plan.get("intro"))
    db.add(company)
    await db.flush()

    # ---- 部门 ----
    dept_by_name: dict[str, Department] = {}
    for i, d in enumerate(plan.get("departments", [])):
        dept = Department(company_id=str(company.id), name=d.get("name") or f"部门{i+1}",
                          description=d.get("description"), sort_order=i * 1000)
        db.add(dept)
        dept_by_name[d.get("name") or f"部门{i+1}"] = dept
    await db.flush()

    # ---- Agent ----
    agent_by_name: dict[str, Agent] = {}
    created_agents = []
    for a in plan.get("agents", []):
        dept = dept_by_name.get(a.get("department") or "")
        agent = Agent(
            id=_uuid.uuid4().hex, company_id=str(company.id),
            department_id=str(dept.id) if dept else None,
            name=a.get("name") or f"员工{len(created_agents)+1}",
            role=a.get("role"), description=a.get("description"),
            system_prompt=a.get("system_prompt"),
            status=AgentStatus.IDLE)
        if a.get("is_leader"):
            agent.manager_agent_id = None
        db.add(agent)
        agent_by_name[a.get("name")] = agent
        created_agents.append({"name": agent.name, "role": agent.role,
                               "department": a.get("department"),
                               "leader": bool(a.get("is_leader"))})
    await db.flush()

    # ---- 上下级关系 ----
    leader_id = None
    for a in plan.get("agents", []):
        ag = agent_by_name.get(a.get("name"))
        if not ag:
            continue
        if a.get("is_leader"):
            leader_id = str(ag.id)
        elif a.get("reports_to"):
            sup = agent_by_name.get(a.get("reports_to"))
            if sup:
                ag.manager_agent_id = str(sup.id)
    if not leader_id and created_agents:
        leader_id = str(list(agent_by_name.values())[0].id)

    # ---- 模型绑定（全部绑同一可用模型）----
    from app.models.agent import AgentModelConfig
    mid = data.model_id
    if not mid:
        from app.models import Model
        row = (await db.execute(select(Model.id).limit(1))).scalar_one_or_none()
        mid = row
    if mid:
        for ag in agent_by_name.values():
            db.add(AgentModelConfig(agent_id=str(ag.id), role="primary", model_id=str(mid)))

    await db.commit()
    return {"company_id": str(company.id), "company_name": company.name,
            "departments": len(dept_by_name), "agents": created_agents,
            "leader_agent_id": leader_id}


@router.post("/preview")
async def preview_blueprint(data: GenerateIn,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """仅解析范本返回蓝图预览，不实际创建。"""
    blueprint = await _llm_plan(db,
        "你是企业组织架构设计专家。根据范本输出建团 JSON 蓝图，只输出 JSON。",
        f"行业：{data.industry or '未指定'}\n公司名：{data.company_name or '请起名'}\n范本：\n{data.blueprint}")
    return {"blueprint": _normalize_plan(blueprint)}
