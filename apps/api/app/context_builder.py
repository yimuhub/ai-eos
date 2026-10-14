"""Project Context System (P2-08).

Context Builder: assembles complete project context (project + milestones +
tasks + team + recent meetings/decisions) for injecting into Agent prompts.
Snapshot: persists a frozen copy (project_context_snapshots, versioned).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting import (Meeting, MeetingActionItem, MeetingDecision)
from app.models.project import (Milestone, Project, ProjectContextSnapshot,
                                ProjectMember)
from app.models.task import Task


async def build_project_context(db: AsyncSession, project_id: str,
                                kb_query: str | None = None,
                                max_kb_chars: int = 1500) -> dict:
    """聚合项目全部上下文，返回结构化 dict。"""
    project = await db.get(Project, project_id)
    if not project:
        return {}

    ms_q = await db.execute(select(Milestone).where(
        Milestone.project_id == project_id))
    milestones = [{"title": m.name, "status": m.status.value,
                   "target_date": m.target_date}
                  for m in ms_q.scalars().all()]

    task_q = await db.execute(select(Task).where(
        Task.project_id == project_id).order_by(
        Task.created_at.desc()).limit(20))
    tasks = [{"id": t.id, "title": t.title, "status": t.status.value,
              "assignee_agent_id": getattr(t, "assignee_agent_id", None)}
             for t in task_q.scalars().all()]

    team_q = await db.execute(select(ProjectMember).where(
        ProjectMember.project_id == project_id))
    team = [{"member_type": m.member_type, "member_id": m.member_id,
             "role": m.role_in_project}
            for m in team_q.scalars().all()]

    meet_q = await db.execute(select(Meeting).where(
        Meeting.project_id == project_id).order_by(
        Meeting.created_at.desc()).limit(3))
    meetings = []
    for m in meet_q.scalars().all():
        dec_q = await db.execute(select(MeetingDecision).where(
            MeetingDecision.meeting_id == m.id))
        decisions = [d.content for d in dec_q.scalars().all()]
        act_q = await db.execute(select(MeetingActionItem).where(
            MeetingActionItem.meeting_id == m.id))
        actions = [{"content": a.content, "status": a.status.value}
                   for a in act_q.scalars().all()]
        meetings.append({"id": m.id, "title": m.title,
                         "status": m.status.value,
                         "decisions": decisions[:5], "actions": actions[:5]})

    ctx = {
        "project": {
            "id": project.id, "name": project.name,
            "description": project.description,
            "status": project.status.value,
            "priority": project.priority,
        },
        "milestones": milestones,
        "tasks": tasks,
        "team": team,
        "meetings": meetings,
    }

    if kb_query:
        from app.knowledge_engine import rag_context
        kb_text = await rag_context(db, project.company_id, kb_query,
                                    top_k=3, max_chars=max_kb_chars,
                                    project_id=project_id, limit=200)
        if kb_text:
            ctx["knowledge"] = kb_text

    return ctx


def render_context_prompt(ctx: dict) -> str:
    """把结构化上下文渲染成可注入 prompt 的文本。"""
    p = ctx.get("project", {})
    lines = [
        f"# 项目上下文：{p.get('name', '未知项目')}",
        f"状态：{p.get('status', '?')}  优先级：{p.get('priority', '-')}",
    ]
    if p.get("description"):
        lines.append(f"简介：{p['description']}")

    if ctx.get("milestones"):
        lines.append("\n## 里程碑")
        for m in ctx["milestones"]:
            due = f"（目标 {m['target_date']}）" if m.get("target_date") else ""
            lines.append(f"- [{m['status']}] {m['title']}{due}")

    if ctx.get("team"):
        lines.append("\n## 团队")
        for t in ctx["team"]:
            lines.append(f"- [{t['member_type']}] {t['member_id'][:8]}"
                         + (f" 角色:{t['role']}" if t.get("role") else ""))

    if ctx.get("tasks"):
        lines.append("\n## 最近任务")
        for t in ctx["tasks"]:
            assignee = (f" → agent:{t['assignee_agent_id'][:8]}"
                        if t.get("assignee_agent_id") else "")
            lines.append(f"- [{t['status']}] {t['title']}{assignee}")

    if ctx.get("meetings"):
        lines.append("\n## 近期会议与决策")
        for mt in ctx["meetings"]:
            lines.append(f"### {mt['title']} [{mt['status']}]")
            for d in mt.get("decisions", []):
                lines.append(f"- 决策: {d}")
            for a in mt.get("actions", []):
                lines.append(f"- 行动项[{a['status']}]: {a['content']}")

    if ctx.get("knowledge"):
        lines.append("\n## 相关知识")
        lines.append(ctx["knowledge"])

    return "\n".join(lines)


async def save_snapshot(db: AsyncSession, project_id: str) -> ProjectContextSnapshot:
    """构建并持久化项目上下文快照（版本号自增）。"""
    from sqlalchemy import func

    ctx = await build_project_context(db, project_id)
    ver_q = await db.execute(select(func.max(ProjectContextSnapshot.version)).where(
        ProjectContextSnapshot.project_id == project_id))
    max_ver = ver_q.scalar() or 0
    snap = ProjectContextSnapshot(
        id=str(uuid.uuid4()),
        project_id=project_id,
        version=max_ver + 1,
        summary=ctx.get("project", {}).get("name"),
        context_data=ctx,
    )
    db.add(snap)
    await db.commit()
    return snap
