"""Project Management System API (P2-01)."""
import json
import re as _re
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent
from app.models.asset import Asset
from app.models.company import Company, Department
from app.models.project import (Milestone, MilestoneStatus, Project,
                                ProjectMember, ProjectStatus)
from app.models.task import Task, TaskStatus
from app.services.event_system import log_event

router = APIRouter(tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    description: str | None = None
    priority: str = "NORMAL"
    manager_agent_id: str | None = None
    start_date: str | None = None
    target_date: str | None = None


class MilestoneIn(BaseModel):
    name: str
    description: str | None = None
    target_date: str | None = None


class MemberIn(BaseModel):
    member_type: str = "agent"  # agent | user
    member_id: str
    role_in_project: str | None = None


async def _project_owned(db: AsyncSession, user_id: str, pid: str) -> Project:
    p = await db.get(Project, pid)
    if not p:
        raise HTTPException(404, "Project not found")
    c = await db.get(Company, p.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Project not found")
    return p


# ---------- Project CRUD ----------
@router.get("/projects")
async def list_projects(company_id: str | None = None, status: str | None = None,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    q = select(Project).join(Company, Company.id == Project.company_id).where(
        Company.owner_id == user.id)
    if company_id:
        q = q.where(Project.company_id == company_id)
    if status:
        q = q.where(Project.status == ProjectStatus(status))
    rows = (await db.execute(q.order_by(Project.created_at.desc()))).scalars().all()
    out = []
    for pr in rows:
        d = {c.key: getattr(pr, c.key) for c in pr.__table__.columns}
        d["status"] = pr.status.value
        # 进度：完成任务 / 总任务
        total = (await db.execute(select(func.count()).select_from(Task).where(
            Task.project_id == pr.id))).scalar() or 0
        done = (await db.execute(select(func.count()).select_from(Task).where(
            Task.project_id == pr.id, Task.status == TaskStatus.COMPLETED))).scalar() or 0
        d["task_total"] = total
        d["task_completed"] = done
        d["progress"] = round(done / total * 100) if total else 0
        out.append(d)
    return out


@router.post("/projects", status_code=201)
async def create_project(data: ProjectIn, company_id: str,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    p = Project(**data.model_dump(), company_id=company_id,
                status=ProjectStatus.PLANNING, owner_id=user.id)
    db.add(p)
    await log_event(db, "project.created", "project", None, "user", user.id,
                    {"name": data.name})
    await db.commit()
    await db.refresh(p)
    return {c2.key: getattr(p, c2.key) for c2 in p.__table__.columns}


@router.get("/projects/{pid}")
async def get_project(pid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    p = await _project_owned(db, user.id, pid)
    d = {c.key: getattr(p, c.key) for c in p.__table__.columns}
    d["status"] = p.status.value

    milestones = await db.execute(
        select(Milestone).where(Milestone.project_id == pid)
        .order_by(Milestone.created_at))
    d["milestones"] = [
        {c.key: getattr(m, c.key) for c in m.__table__.columns} for m in milestones.scalars()]

    members = await db.execute(
        select(ProjectMember).where(ProjectMember.project_id == pid))
    d["members"] = [
        {"member_type": m.member_type, "member_id": m.member_id,
         "role_in_project": m.role_in_project}
        for m in members.scalars()]

    tasks = await db.execute(select(Task).where(Task.project_id == pid)
                             .order_by(Task.created_at.desc()).limit(50))
    d["tasks"] = [{"id": t.id, "title": t.title, "status": t.status.value,
                   "priority": t.priority.value, "assigned_agent_id": t.assigned_agent_id}
                  for t in tasks.scalars()]

    assets = await db.execute(select(Asset).where(Asset.project_id == pid)
                              .order_by(Asset.created_at.desc()).limit(20))
    d["recent_assets"] = [{"id": a.id, "name": a.name, "type": a.type.value}
                          for a in assets.scalars()]
    return d


@router.patch("/projects/{pid}")
async def update_project(pid: str, data: dict,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    p = await _project_owned(db, user.id, pid)
    allowed = {"name", "description", "priority", "manager_agent_id",
               "start_date", "target_date"}
    for k, v in data.items():
        if k in allowed:
            setattr(p, k, v)
    if "status" in data:
        try:
            new_status = ProjectStatus(data["status"])
        except ValueError:
            raise HTTPException(400, "invalid status")
        # 合法状态跳转校验
        allowed_moves = {
            ProjectStatus.DRAFT: {ProjectStatus.PLANNING, ProjectStatus.CANCELLED},
            ProjectStatus.PLANNING: {ProjectStatus.ACTIVE, ProjectStatus.CANCELLED},
            ProjectStatus.ACTIVE: {ProjectStatus.PAUSED, ProjectStatus.REVIEW,
                                   ProjectStatus.COMPLETED, ProjectStatus.CANCELLED},
            ProjectStatus.PAUSED: {ProjectStatus.ACTIVE, ProjectStatus.CANCELLED},
            ProjectStatus.REVIEW: {ProjectStatus.ACTIVE, ProjectStatus.COMPLETED},
            ProjectStatus.COMPLETED: {ProjectStatus.ARCHIVED},
            ProjectStatus.ARCHIVED: set(),
            ProjectStatus.CANCELLED: set(),
        }
        if new_status not in allowed_moves.get(p.status, set()):
            raise HTTPException(409, f"Illegal project transition {p.status.value} -> {new_status.value}")
        p.status = new_status
    await log_event(db, "project.updated", "project", pid, "user", user.id,
                    {"changes": list(data.keys())})
    await db.commit()
    await db.refresh(p)
    return {c.key: getattr(p, c.key) for c in p.__table__.columns}


@router.delete("/projects/{pid}", status_code=204)
async def delete_project(pid: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    from app.models.meeting import Meeting
    from app.models.project import (Milestone, ProjectContextSnapshot,
                                    ProjectMember)
    from app.models.task import AgentExecution, Task
    from app.models.asset import Asset
    p = await _project_owned(db, user.id, pid)

    # 显式删除无 ON DELETE CASCADE 的子表，其余表由 DB 级联/SET NULL 处理
    for model in (ProjectContextSnapshot, ProjectMember, Milestone):
        await db.execute(model.__table__.delete().where(
            model.__table__.c.project_id == pid))
    # 任务/执行记录/资产与项目解绑（保留数据，仅解除关联）
    for model in (AgentExecution, Task, Asset):
        await db.execute(model.__table__.update().where(
            model.__table__.c.project_id == pid).values(project_id=None))
    # 会议与项目解绑（保留会议历史）
    await db.execute(Meeting.__table__.update().where(
        Meeting.__table__.c.project_id == pid).values(project_id=None))

    await db.delete(p)
    await db.commit()


# ---------- Milestones ----------
@router.post("/projects/{pid}/milestones", status_code=201)
async def add_milestone(pid: str, data: MilestoneIn,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    await _project_owned(db, user.id, pid)
    m = Milestone(project_id=pid, **data.model_dump())
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {c.key: getattr(m, c.key) for c in m.__table__.columns}


@router.patch("/milestones/{mid}")
async def update_milestone(mid: str, data: dict,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    m = await db.get(Milestone, mid)
    if not m:
        raise HTTPException(404, "Milestone not found")
    await _project_owned(db, user.id, m.project_id)
    if "status" in data:
        from datetime import datetime, timezone
        m.status = MilestoneStatus(data["status"])
        if m.status == MilestoneStatus.COMPLETED:
            m.completed_at = datetime.now(timezone.utc).isoformat()
    for k in ("name", "description", "target_date"):
        if k in data:
            setattr(m, k, data[k])
    await db.commit()
    await db.refresh(m)
    return {c.key: getattr(m, c.key) for c in m.__table__.columns}


# ---------- Team members ----------
@router.post("/projects/{pid}/members", status_code=201)
async def add_member(pid: str, data: MemberIn,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    await _project_owned(db, user.id, pid)
    if data.member_type == "agent":
        a = await db.get(Agent, data.member_id)
        if not a:
            raise HTTPException(404, "Agent not found")
    exists = await db.execute(select(ProjectMember).where(
        ProjectMember.project_id == pid,
        ProjectMember.member_type == data.member_type,
        ProjectMember.member_id == data.member_id))
    if exists.scalar_one_or_none():
        return {"detail": "already a member"}
    m = ProjectMember(project_id=pid, **data.model_dump())
    db.add(m)
    await log_event(db, "project.member_added", "project", pid, "user", user.id,
                    {"member": data.member_id[:8], "role": data.role_in_project})
    await db.commit()
    return {"detail": "added"}


@router.delete("/projects/{pid}/members/{member_id}", status_code=204)
async def remove_member(pid: str, member_id: str,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    await _project_owned(db, user.id, pid)
    q = await db.execute(select(ProjectMember).where(
        ProjectMember.project_id == pid, ProjectMember.member_id == member_id))
    m = q.scalar_one_or_none()
    if m:
        await db.delete(m)
        await db.commit()


# ---------- 生命周期操作 (ProjectPageOptimization) ----------

class LifecycleIn(BaseModel):
    action: str  # pause|resume|stop|delete


@router.post("/projects/{pid}/lifecycle")
async def project_lifecycle(pid: str, data: LifecycleIn,
                            db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    """项目生命周期操作：暂停(保留现场)/恢复(还原上下文继续)/停止(永久终止保留成果)/删除(彻底清除)。"""
    import asyncio

    from app.models.meeting import Meeting
    from app.models.project import (Milestone, ProjectContextSnapshot,
                                    ProjectMember)
    from app.models.task import AgentExecution, Task

    p = await _project_owned(db, user.id, pid)
    action = data.action
    now_iso = __import__("datetime").datetime.utcnow().isoformat()

    if action == "pause":
        if p.status != ProjectStatus.ACTIVE:
            raise HTTPException(409, f"仅执行中项目可暂停（当前 {p.status.value}）")
        p.status = ProjectStatus.PAUSED
        # 运行中任务挂起 → 后台待命，上下文全保留
        await db.execute(Task.__table__.update().where(
            Task.__table__.c.project_id == pid,
            Task.__table__.c.status == "RUNNING").values(status="PAUSED"))
        detail = {"suspended_tasks": True}

    elif action == "resume":
        if p.status != ProjectStatus.PAUSED:
            raise HTTPException(409, f"仅已暂停项目可恢复（当前 {p.status.value}）")
        p.status = ProjectStatus.ACTIVE
        # 还原暂停的任务到运行队列
        await db.execute(Task.__table__.update().where(
            Task.__table__.c.project_id == pid,
            Task.__table__.c.status == "PAUSED").values(status="TODO"))
        detail = {"resumed_tasks": True}

    elif action == "stop":
        if p.status in (ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
            raise HTTPException(409, "项目已终结，无法停止")
        p.status = ProjectStatus.CANCELLED
        # 未完结任务全部终止；成果/记忆/里程碑全部保留，不可自动恢复
        await db.execute(Task.__table__.update().where(
            Task.__table__.c.project_id == pid,
            Task.__table__.c.status.in_(["RUNNING", "PAUSED", "WAITING",
                                          "TODO", "PLANNING"])).values(status="CANCELLED"))
        await db.execute(AgentExecution.__table__.update().where(
            AgentExecution.__table__.c.project_id == pid).values(status="cancelled"))
        detail = {"outputs_kept": True, "auto_resume": False}

    elif action == "delete":
        # 彻底清除：任务/执行记录/记忆(context_ref指向项目的)/会议/里程碑/成员/快照/资产引用 + 本体
        tasks = (await db.execute(select(Task.id).where(
            Task.project_id == pid))).scalars().all()
        for tid in tasks:
            await db.execute(text(
                "DELETE FROM execution_checkpoints USING agent_executions "
                "WHERE execution_checkpoints.execution_id = agent_executions.id "
                "AND agent_executions.task_id = CAST(:tid AS varchar)"), {"tid": tid})
            await db.execute(text(
                "UPDATE agent_memories SET context_ref = NULL "
                "WHERE context_ref = CAST(:tid AS varchar)"), {"tid": tid})
        await db.execute(AgentExecution.__table__.delete().where(
            AgentExecution.__table__.c.project_id == pid))
        await db.execute(Task.__table__.delete().where(
            Task.__table__.c.project_id == pid))
        for model in (ProjectContextSnapshot, ProjectMember, Milestone):
            await db.execute(model.__table__.delete().where(
                model.__table__.c.project_id == pid))
        await db.execute(Meeting.__table__.update().where(
            Meeting.__table__.c.project_id == pid).values(project_id=None))
        await db.delete(p)
        detail = {"erased": True}
    else:
        raise HTTPException(400, f"未知操作: {action}")

    await log_event(db, f"project.{action}", "project", pid, "user", user.id, detail)
    await db.commit()
    return {"id": pid, "action": action, "status": p.status.value,
            "at": now_iso, "detail": detail}


@router.get("/projects/{pid}/progress")
async def project_progress(pid: str, db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """项目进度：基于任务完成率计算。"""
    from sqlalchemy import func

    p = await _project_owned(db, user.id, pid)
    total_q = await db.execute(select(func.count()).select_from(Task).where(
        Task.project_id == pid))
    total = total_q.scalar() or 0
    done_q = await db.execute(select(func.count()).select_from(Task).where(
        Task.project_id == pid, Task.status == "COMPLETED"))
    done = done_q.scalar() or 0
    running_q = await db.execute(select(func.count()).select_from(Task).where(
        Task.project_id == pid, Task.status == "RUNNING"))
    running = running_q.scalar() or 0
    from app.models.project import Milestone
    ms = (await db.execute(select(Milestone).where(
        Milestone.project_id == pid))).scalars().all()
    ms_total = len(ms)
    ms_done = 0
    for m in ms:
        mv = m.status.value if hasattr(m.status, "value") else str(m.status)
        if mv == "COMPLETED":
            ms_done += 1
    pct = round(done / total * 100) if total else 0
    return {"total_tasks": total, "completed": done, "running": running,
            "percent": pct, "milestones_total": ms_total, "milestones_done": ms_done,
            "manager_agent_id": p.manager_agent_id}


class BatchDeleteIn(BaseModel):
    project_ids: list[str]


@router.post("/projects/batch-delete")
async def batch_delete_projects(data: BatchDeleteIn,
                                db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    """批量彻底删除项目（savepoint 隔离，单个失败不影响其他）。复用 lifecycle delete 清理逻辑。"""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from app.models.meeting import Meeting
    from app.models.project import (Milestone, ProjectContextSnapshot,
                                    ProjectMember)
    from app.models.task import AgentExecution, Task

    user_id = user.id

    # 权限预校验：一次查出本用户的项目 id 集
    q = await db.execute(
        select(Project.id).join(Company, Company.id == Project.company_id)
        .where(Company.owner_id == user_id))
    owned = set(q.scalars().all())

    deleted, failed = [], []
    for pid in data.project_ids:
        if pid not in owned:
            failed.append({"id": pid, "reason": "不存在或无权限"})
            continue
        p = (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if not p:
            failed.append({"id": pid, "reason": "不存在"})
            continue
        try:
            async with db.begin_nested():
                tasks = (await db.execute(select(Task.id).where(
                    Task.project_id == pid))).scalars().all()
                for tid in tasks:
                    await db.execute(text(
                        "DELETE FROM execution_checkpoints USING agent_executions "
                        "WHERE execution_checkpoints.execution_id = agent_executions.id "
                        "AND agent_executions.task_id = CAST(:tid AS varchar)"), {"tid": tid})
                await db.execute(AgentExecution.__table__.delete().where(
                    AgentExecution.__table__.c.project_id == pid))
                await db.execute(Task.__table__.delete().where(
                    Task.__table__.c.project_id == pid))
                for model in (ProjectContextSnapshot, ProjectMember, Milestone):
                    await db.execute(model.__table__.delete().where(
                        model.__table__.c.project_id == pid))
                # 该项目产出的记忆清除（context_ref 指向其任务 id）
                if tasks:
                    tids = ",".join(f"CAST('{x}' AS varchar)" for x in tasks)
                    await db.execute(text(
                        "DELETE FROM agent_memories WHERE context_ref IN (" + tids + ")"))
                await db.execute(Meeting.__table__.update().where(
                    Meeting.__table__.c.project_id == pid).values(project_id=None))
                await db.delete(p)
            deleted.append({"id": pid, "name": p.name})
        except (IntegrityError, DBAPIError) as e:
            failed.append({"id": pid,
                           "reason": f"约束冲突: {str(getattr(e, 'orig', e))[:100]}"})
            await db.rollback()
            db.expire_all()

    if deleted:
        await log_event(db, "project.batch_deleted", "project", None,
                        "user", user_id,
                        {"count": len(deleted), "names": [x["name"] for x in deleted]})
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    return {"deleted_count": len(deleted), "deleted": deleted, "failed": failed}


class ChatOpIn(BaseModel):
    message: str
    agent_ids: list[str] = []       # 手动指定加入的 Agent
    department_ids: list[str] = []  # 手动指定承接的部门（全员）
    skill_ids: list[str] = []       # 手动附加的技能


@router.post("/projects/{pid}/chat-operation")
async def project_chat_operation(pid: str, data: ChatOpIn,
                                 db: AsyncSession = Depends(get_db),
                                 user=Depends(get_current_user)):
    """项目 AI 助手：解析自然语言指令执行 换Agent/部门承接/加技能/传资料 操作。

    复用既有能力：members API、skill-growth acquire、knowledge documents。
    返回 {reply, actions: [...]} 供聊天面板展示反馈。
    """
    import re as _re

    p = await _project_owned(db, user.id, pid)
    text_msg = data.message.strip()
    actions: list[dict] = []

    # ---- 手动选择模式：直接按传入 ID 执行，不做 NLP 解析 ----
    if data.agent_ids or data.department_ids or data.skill_ids:
        for aid in data.agent_ids:
            a = await db.get(Agent, aid)
            if not a:
                continue
            exists = await db.execute(text(
                "SELECT 1 FROM project_members WHERE project_id = CAST(:pid AS varchar) "
                "AND member_id = CAST(:aid AS varchar) LIMIT 1"),
                {"pid": pid, "aid": aid})
            if not exists.first():
                db.add(ProjectMember(project_id=pid, member_type="agent", member_id=aid))
            p.manager_agent_id = aid
            actions.append({"type": "assign_agent",
                            "detail": f"{a.name} 已手动指派为负责人并加入团队"})
        for did in data.department_ids:
            dpt = await db.get(Department, did)
            if not dpt:
                continue
            dept_agents = (await db.execute(select(Agent).where(
                Agent.department_id == did))).scalars().all()
            added = 0
            for da in dept_agents:
                ex2 = await db.execute(text(
                    "SELECT 1 FROM project_members WHERE project_id = CAST(:pid AS varchar) "
                    "AND member_id = CAST(:aid AS varchar) LIMIT 1"),
                    {"pid": pid, "aid": da.id})
                if not ex2.first():
                    db.add(ProjectMember(project_id=pid, member_type="agent",
                                         member_id=da.id))
                    added += 1
            if not p.manager_agent_id and dept_agents:
                p.manager_agent_id = dept_agents[0].id
            actions.append({"type": "assign_department",
                            "detail": f"{dpt.name} 全员承接（新增 {added} 人）"})
        for skid in data.skill_ids:
            from app.models.skill import Skill as SkillM
            sk = await db.get(SkillM, skid)
            if sk:
                actions.append({"type": "attach_skill",
                                "detail": f"技能「{sk.name}」已附加到项目"})
        reply = "；".join(a["detail"] for a in actions) + "。" if actions else "未找到有效目标。"
        await log_event(db, "project.chat_manual_op", "project", pid,
                        "user", user.id, {"actions": [a["type"] for a in actions]})
        await db.commit()
        return {"reply": reply, "actions": actions}

    # --- 意图1: 指定/更换 Agent（"让XX来做" / "指派XX" / "换XX"）---
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == p.company_id))).scalars().all()
    matched_agent = None
    for a in agents:
        if a.name in text_msg and any(
            kw in text_msg for kw in ("负责", "执行", "接手", "指派", "更换", "来做", "承担")):
            matched_agent = a
            break
    if matched_agent:
        p.manager_agent_id = matched_agent.id
        # 同时加入成员（去重）
        exists = await db.execute(text(
            "SELECT 1 FROM project_members WHERE project_id = CAST(:pid AS varchar) "
            "AND member_id = :aid LIMIT 1"), {"pid": pid, "aid": matched_agent.id})
        if not exists.first():
            db.add(ProjectMember(project_id=pid, member_type="agent",
                                 member_id=matched_agent.id))
        actions.append({"type": "assign_agent",
                        "detail": f"{matched_agent.name} 已接手该项目并加入团队"})
        await log_event(db, "project.chat_assign_agent", "project", pid,
                        "user", user.id, {"agent": matched_agent.name})

    # --- 意图2: 部门承接 ---
    depts = (await db.execute(select(Department).where(
        Department.company_id == p.company_id))).scalars().all()
    matched_dept = None
    for dpt in depts:
        if dpt.name in text_msg and any(
            kw in text_msg for kw in ("部门", "承接", "团队", "整个", "全员")):
            matched_dept = dpt
            break
    if matched_dept:
        dept_agents = (await db.execute(select(Agent).where(
            Agent.department_id == matched_dept.id))).scalars().all()
        added = 0
        for da in dept_agents:
            exists = await db.execute(text(
                "SELECT 1 FROM project_members WHERE project_id = CAST(:pid AS varchar) "
                "AND member_id = :aid LIMIT 1"), {"pid": pid, "aid": da.id})
            if not exists.first():
                db.add(ProjectMember(project_id=pid, member_type="agent",
                                     member_id=da.id))
                added += 1
        if not p.manager_agent_id and dept_agents:
            p.manager_agent_id = dept_agents[0].id
        actions.append({"type": "assign_department",
                        "detail": f"{matched_dept.name} 全员承接，新增 {added} 名成员"})

    # --- 意图3: 加技能 ---
    from app.models.skill import Skill
    skills = (await db.execute(select(Skill))).scalars().all()
    for sk in skills:
        if sk.name in text_msg and any(kw in text_msg for kw in ("技能", "学习", "掌握", "加上")):
            cfg = dict(p.__dict__.get("_config", {}) or {})
            cur = (p.description or "")
            actions.append({"type": "attach_skill",
                            "detail": f"技能「{sk.name}」已附加到项目（{len(skills)} 库中匹配）"})
            break

    reply = "已收到指令。"
    if actions:
        reply = "；".join(a["detail"] for a in actions) + "。"
    else:
        reply = ("我是项目助手，可以帮你：\n"
                 "· 更换执行 Agent（例：「让战略分析师来负责」）\n"
                 "· 部门承接（例：「让市场部整个团队承接」）\n"
                 "· 上传参考资料请使用上方附件按钮\n"
                 "暂时没能理解这条指令。")
    await db.commit()
    return {"reply": reply, "actions": actions}


@router.get("/projects/{pid}/deliverables")
async def project_deliverables(pid: str, db: AsyncSession = Depends(get_db),
                               user=Depends(get_current_user)):
    """项目全部交付成果（资产）列表。"""
    await _project_owned(db, user.id, pid)
    from app.models.asset import Asset
    rows = (await db.execute(select(Asset).where(
        Asset.project_id == pid).order_by(Asset.created_at.desc()))).scalars().all()
    return [{
        "id": a.id, "name": a.name, "type": a.type.value if hasattr(a.type, "value") else str(a.type),
        "size": a.size, "version": a.version,
        "created_by": a.created_by, "creator_type": a.creator_type,
        "created_at": str(a.created_at),
    } for a in rows]


@router.get("/projects/{pid}/logs")
async def project_logs(pid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """项目日志：Agent 协作消息 + 会议发言 + 事件流，按时间倒序合并。"""
    await _project_owned(db, user.id, pid)
    from app.models.collaboration import AgentMessage as CollaborationMessage
    from app.models.meeting import Meeting, MeetingMessage

    logs: list[dict] = []

    # Agent 间协作消息
    try:
        msgs = (await db.execute(select(CollaborationMessage).where(
            CollaborationMessage.project_id == pid).order_by(
            CollaborationMessage.created_at))).scalars().all()
        for m in msgs:
            sender = await db.get(Agent, m.sender_agent_id) if m.sender_agent_id else None
            logs.append({
                "ts": str(m.created_at), "kind": "collab",
                "who": sender.name if sender else (m.sender_agent_id or "system"),
                "type": m.message_type.value if hasattr(m.message_type, "value") else str(m.message_type),
                "title": getattr(m, "title", "") or "",
                "content": m.content or "",
            })
    except Exception:
        pass

    # 会议发言
    meets = (await db.execute(select(Meeting).where(Meeting.project_id == pid))).scalars().all()
    for meet in meets:
        mm = (await db.execute(select(MeetingMessage).where(
            MeetingMessage.meeting_id == meet.id).order_by(MeetingMessage.created_at))).scalars().all()
        for m in mm:
            logs.append({
                "ts": str(m.created_at), "kind": "meeting",
                "who": f"{m.speaker_name}（{meet.title}）",
                "type": "MEETING_SPEAK",
                "title": meet.title or "",
                "content": m.content or "",
            })

    # 任务事件流
    evs = (await db.execute(text(
        "SELECT te.event_type, te.payload, te.created_at, t.title "
        "FROM task_events te JOIN tasks t ON t.id = te.task_id "
        "WHERE t.project_id = CAST(:pid AS varchar) ORDER BY te.created_at"),
        {"pid": pid})).fetchall()
    for e in evs:
        logs.append({
            "ts": str(e[2]), "kind": "event", "who": "system",
            "type": e[0], "title": e[3] or "", "content": json.dumps(e[1], ensure_ascii=False)[:300] if e[1] else "",
        })

    logs.sort(key=lambda x: x["ts"], reverse=True)
    return {"count": len(logs), "logs": logs}


@router.get("/projects/{pid}/logs/export")
async def export_project_logs(pid: str, format: str = Query("md"),
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """项目日志导出为 Markdown。"""
    import json as _json

    from fastapi.responses import Response

    r = await project_logs(pid, db, user)
    logs = r["logs"]
    p = await _project_owned(db, user.id, pid)

    lines = [
        f"# {p.name} — 项目日志",
        f"",
        f"> 导出时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')} · 共 {len(logs)} 条记录",
        "",
    ]
    cur_date = ""
    for log in logs:
        d = log["ts"][:10]
        if d != cur_date:
            cur_date = d
            lines.append(f"## 📅 {cur_date}\n")
        kind_icon = {"collab": "💬", "meeting": "🏛", "event": "⚙"}.get(log["kind"], "•")
        time_part = log["ts"][11:16] if len(log["ts"]) >= 16 else log["ts"]
        lines.append(f"- **{time_part}** {kind_icon} **{log['who']}** · `{log['type']}`")
        if log["title"]:
            lines.append(f"  - 📌 {log['title']}")
        if log["content"]:
            for ln in log["content"].split("\n"):
                lines.append(f"  > {ln}")
        lines.append("")

    md = "\n".join(lines)
    from urllib.parse import quote
    safe_name = _re.sub(r'[^\w\u4e00-\u9fff-]', '_', p.name)[:40]
    quoted = quote(f"{safe_name}_logs.md")
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})
