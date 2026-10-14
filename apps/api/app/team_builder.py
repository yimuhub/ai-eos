"""Team Builder engine (P2-12).

按模板自动为项目组建团队：匹配公司内 Agent 的 role 描述与模板角色，
将最合适的 Agent 加入 project_members。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.project import Project, ProjectMember

# 团队模板：角色 -> 匹配关键词（对 Agent.name/role 做包含匹配）
TEAM_TEMPLATES = {
    "software": {
        "name": "软件项目团队",
        "roles": [
            {"keywords": ["CEO", "总裁", "负责人"], "role_in_project": "项目负责人"},
            {"keywords": ["程序员", "开发", "工程师"], "role_in_project": "开发工程师"},
            {"keywords": ["战略", "架构"], "role_in_project": "技术顾问"},
            {"keywords": ["市场", "运营"], "role_in_project": "市场联络"},
        ],
    },
    "wenlv": {
        "name": "文旅策划团队",
        "roles": [
            {"keywords": ["CEO", "总监"], "role_in_project": "项目总监"},
            {"keywords": ["战略", "策划"], "role_in_project": "策划师"},
            {"keywords": ["市场", "调研"], "role_in_project": "市场分析师"},
            {"keywords": ["财务", "测算", "会计"], "role_in_project": "投资测算"},
            {"keywords": ["设计"], "role_in_project": "设计师"},
        ],
    },
    "research": {
        "name": "研究团队",
        "roles": [
            {"keywords": ["CEO"], "role_in_project": "项目负责人"},
            {"keywords": ["战略", "分析", "研究"], "role_in_project": "研究员"},
            {"keywords": ["市场", "调研"], "role_in_project": "数据收集"},
        ],
    },
}


def _match_agent(agents: list[Agent], keywords: list[str],
                 taken_ids: set[str]) -> Agent | None:
    """按关键词匹配未占用的 Agent。"""
    for kw in keywords:
        for a in agents:
            if a.id in taken_ids:
                continue
            name_role = f"{a.name} {a.role or ''}"
            if kw.lower() in name_role.lower():
                return a
    return None


async def build_team_for_project(db: AsyncSession, project_id: str,
                                 template_key: str | None = None) -> dict | None:
    """按模板为项目组建团队，返回加入的成员列表。"""
    import uuid

    project = await db.get(Project, project_id)
    if not project:
        return None

    # 选模板：优先显式指定；否则根据项目名猜（文旅/软件/默认研究）
    if template_key and template_key in TEAM_TEMPLATES:
        tpl = TEAM_TEMPLATES[template_key]
    else:
        pname = (project.name or "") + (project.description or "")
        if any(k in pname for k in ("文旅", "景区", "旅游")):
            tpl = TEAM_TEMPLATES["wenlv"]
        elif any(k in pname for k in ("系统", "软件", "平台", "App")):
            tpl = TEAM_TEMPLATES["software"]
        else:
            tpl = TEAM_TEMPLATES["research"]

    agents = (await db.execute(select(Agent).where(
        Agent.company_id == project.company_id))).scalars().all()

    # 已有成员不重复加
    existing_q = await db.execute(select(ProjectMember).where(
        ProjectMember.project_id == project_id))
    taken_ids = {m.member_id for m in existing_q.scalars().all()}

    added = []
    for role_spec in tpl["roles"]:
        agent = _match_agent(list(agents), role_spec["keywords"], taken_ids)
        if not agent:
            continue
        member = ProjectMember(
            id=str(uuid.uuid4()),
            project_id=project_id,
            member_type="agent",
            member_id=agent.id,
            role_in_project=role_spec["role_in_project"],
        )
        db.add(member)
        taken_ids.add(agent.id)
        added.append({"agent_id": agent.id, "agent_name": agent.name,
                      "role_in_project": role_spec["role_in_project"]})

    await db.commit()
    return {"project_id": project_id, "template": tpl["name"],
            "added": added, "team_size": len(taken_ids)}
