"""One-phrase Command Deck (V4.0 一句话指挥台).

老板说一句话 → AI 判断走哪条路（方案阶段不落库）→ 老板点头 → 执行：
  DIRECT   简单任务 → 直接派给现有员工（走 /task-dispatch 同款链路）
  EXPERT   需要新工种 → 专家库自动组队（蓝图进工厂，逐个审查）
  PROJECT  大目标 → 作战室全流程（负责人拆解 → 团队执行 → 交付）

执行实现原则：不重写业务，直接调既有引擎/路由核心，
新增的只有"一句话→结构化方案"这一层 LLM 判断。
"""
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.model_gateway import ModelGateway, set_usage_context
from app.models.agent import Agent, AgentModelConfig


async def _first_model_id(db: AsyncSession) -> str:
    mq = await db.execute(select(AgentModelConfig).limit(1))
    m = mq.scalars().first()
    if not m:
        raise RuntimeError("没有可用模型，请先在模型配置页接入")
    return m.model_id


async def command_plan(db: AsyncSession, company_id: str,
                       phrase: str, attachments: list | None = None) -> dict:
    """一句话 → 结构化方案（path + targets/team_goal + 预计耗时），不执行。"""
    mid = await _first_model_id(db)
    gw = ModelGateway(db)
    set_usage_context(company_id=company_id, scenario="COMMAND")

    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    agent_list = "\n".join(
        f"{i+1}. {a.name}（{a.role or '未定岗'}）"
        for i, a in enumerate(agents[:40])) or "（暂无）"

    att_txt = ""
    if attachments:
        att_txt = ("\n参考附件：" +
                   "、".join(str(a.get("name", "")) for a in attachments[:10]))

    sys_prompt = (
        "你是企业指挥台。老板说一句话，你判断执行路径并给出方案。只输出JSON：\n"
        '{"path":"DIRECT|EXPERT|PROJECT","reason":"一句理由",'
        '"targets":[{"agent_id":编号,"task":"该员工负责的事"}],'
        '"team_goal":"交给组队引擎/作战室的一句话目标",'
        '"est_minutes":预计分钟数}\n'
        "判断标准：现有1~3名员工直接能干完=DIRECT；需要库里新工种/跨领域"
        "且产出方案类=EXPERT；多阶段大目标、要交付成品并跟踪过程=PROJECT。"
        "targets.agent_id 必须用名单编号（数字）。")
    result = await gw.chat_with_agent_models(
        [mid],
        [{"role": "system", "content": sys_prompt},
         {"role": "user",
          "content": f"老板原话：{phrase}{att_txt}\n\n现有员工名单：\n{agent_list}"}],
        config={"max_tokens": 700})
    raw = (result.get("content") or "{}").strip()
    s, e = raw.find("{"), raw.rfind("}")
    plan: dict = {}
    if s >= 0 and e > s:
        try:
            plan = json.loads(raw[s:e + 1])
        except Exception:
            plan = {}
    path = plan.get("path")
    if path not in ("DIRECT", "EXPERT", "PROJECT"):
        path = "PROJECT"
    targets = []
    for t in plan.get("targets") or []:
        try:
            idx = int(t.get("agent_id")) - 1
            if 0 <= idx < len(agents):
                targets.append({"agent_id": str(agents[idx].id),
                                "agent_name": agents[idx].name,
                                "task": t.get("task") or phrase[:50]})
        except (TypeError, ValueError):
            continue
    return {"path": path, "reason": plan.get("reason", ""),
            "targets": targets,
            "team_goal": plan.get("team_goal") or phrase,
            "est_minutes": plan.get("est_minutes", 10),
            "notes": plan.get("notes", ""), "phrase": phrase}


async def command_execute(db: AsyncSession, company_id: str,
                          plan: dict, user_id: str) -> dict:
    """老板确认后的方案 → 实际执行（全部走既有引擎）。"""
    path = plan.get("path", "PROJECT")

    if path == "DIRECT":
        from app.api.routes.task_dispatch import DispatchIn, dispatch_tasks
        from app.models.user import User
        user = await db.get(User, user_id)
        outs = []
        for t in plan.get("targets") or []:
            d = DispatchIn(company_id=company_id,
                          title=f"指挥台：{plan.get('phrase', '')[:40]}",
                          description=t.get("task") or plan.get("phrase", ""),
                          target_type="single_agent",
                          agent_ids=[t["agent_id"]])
            r = await dispatch_tasks(d, db=db, user=user)
            outs.append(r if isinstance(r, dict) else {"task_id": str(r)})
        return {"path": "DIRECT", "jobs": outs}

    if path == "EXPERT":
        from app.api.routes.experts import build_team, TeamIn
        from app.models.user import User
        user = await db.get(User, user_id)
        r = await build_team(
            TeamIn(goal=plan.get("team_goal") or plan.get("phrase", ""),
                   headcount=4),
            company_id=company_id, db=db, user=user)
        return {"path": "EXPERT", "jobs": r.get("jobs", []),
                "team_size": r.get("team_size", 0)}

    # PROJECT → 作战室（复用 war_room.kickoff 的核心动作）
    leader_id = (plan.get("targets") or [{}])[0].get("agent_id")
    if not leader_id:
        # 取公司第一个可用 Agent 当负责人
        aq = await db.execute(select(Agent).where(
            Agent.company_id == company_id).limit(1))
        first = aq.scalars().first()
        leader_id = str(first.id) if first else None
    if not leader_id:
        from fastapi import HTTPException
        raise HTTPException(400, "公司里还没有员工，先去「Agent 工厂」或「专家库」造人，再回来下达")
    from app.models.war_room import WarRoomSession
    from app.models.project import Project, ProjectMember, ProjectStatus
    import app.background_jobs as bg
    from app.api.routes.war_room import _make_kickoff

    project_id = plan.get("project_id")
    if not project_id:
        proj = Project(company_id=company_id,
                      name=(plan.get("team_goal") or plan.get("phrase", ""))[:60],
                      description=f"一句话指挥台自动创建 · 目标：{plan.get('phrase','')}",
                      status=ProjectStatus.ACTIVE, priority="NORMAL",
                      owner_id=user_id, manager_agent_id=leader_id)
        db.add(proj)
        await db.flush()
        project_id = proj.id
        db.add(ProjectMember(project_id=project_id, member_type="agent",
                             member_id=leader_id, role_in_project="manager"))

    sid = uuid.uuid4().hex
    sess = WarRoomSession(id=sid, company_id=company_id,
                          project_id=project_id,
                          goal=plan.get("team_goal") or plan.get("phrase", ""),
                          leader_agent_id=leader_id, status="analyzing")
    db.add(sess)
    bg.submit_async(_make_kickoff(company_id, sid,
                                   plan.get("team_goal") or plan.get("phrase", ""),
                                   leader_id, None, project_id))
    await db.commit()
    return {"path": "PROJECT", "session_id": sid, "project_id": project_id}
