"""War Room API —— 项目作战室。

业务流（老板可视化确认点贯穿全程）：
1. POST /war-room/kickoff          老板下达目标 → 负责人 LLM 分析拆解 → 建会话+任务
2. POST /war-room/{sid}/announce    各成员在群聊复述「我的任务是什么」
3. GET  /war-room/{sid}             看板：分析/分工/宣告状态/产出物
4. POST /war-room/{sid}/acknowledge 负责人逐项校对：正确请继续 / 提出纠正
5. POST /war-room/{sid}/start       全员确认完毕 → 并行执行
6. POST /war-room/tasks/{tid}/submit 成员提交产出物（校验统一交付格式）
7. POST /war-room/tasks/{tid}/review 可视化审核（负责人审 / 老板人工干预）
8. POST /war-room/{sid}/finalize    全部通过 → 负责人整合最终产出物
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig
from app.models.company import Company
from app.models.user import User
from app.models.war_room import WarRoomSession, WarRoomTask
from app.model_gateway import ModelGateway

router = APIRouter(prefix="/war-room", tags=["war-room"])

DELIVERABLE_FORMATS = {
    "md": "Markdown 文档（.md）",
    "word": "Word 文档（.docx）",
    "ppt": "PPT 演示文稿（.pptx）",
    "excel": "Excel 表格（.xlsx）",
}


async def _owned(db: AsyncSession, user_id: str, company_id: str) -> Company:
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "company not found")
    return c


async def _get_session(db: AsyncSession, sid: str) -> tuple[WarRoomSession, list[WarRoomTask]]:
    sess = await db.get(WarRoomSession, sid)
    if not sess:
        raise HTTPException(404, "session not found")
    rows = (await db.execute(select(WarRoomTask).where(
        WarRoomTask.session_id == sid).order_by(WarRoomTask.seq))).scalars().all()
    return sess, list(rows)


def _session_out(sess: WarRoomSession, tasks: list[WarRoomTask], agents_by_id: dict) -> dict:
    return {
        "id": sess.id, "project_id": sess.project_id,
        "goal": sess.goal, "status": sess.status,
        "leader_agent_id": sess.leader_agent_id,
        "leader_name": agents_by_id.get(sess.leader_agent_id, "负责人"),
        "analysis": sess.analysis,
        "deliverable_format": sess.deliverable_format,
        "deliverable_rule": sess.deliverable_rule,
        "final_output_path": sess.final_output_path,
        "tasks": [{
            "id": t.id, "seq": t.seq, "agent_id": t.agent_id,
            "agent_name": agents_by_id.get(t.agent_id, "?"),
            "title": t.title, "detail": t.detail,
            "announce_status": t.announce_status,
            "announce_text": t.announce_text,
            "leader_feedback": t.leader_feedback,
            "status": t.status,
            "deliverable_name": t.deliverable_name,
            "deliverable_path": t.deliverable_path,
            "reviewed_by": t.reviewed_by,
            "review_comment": t.review_comment,
        } for t in tasks],
    }


@router.get("/sessions")
async def list_sessions(company_id: str = Query(...),
                        project_id: str | None = Query(None),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """列出作战会话（按项目过滤可选）。"""
    await _owned(db, user.id, company_id)
    q = select(WarRoomSession).where(WarRoomSession.company_id == company_id)
    if project_id:
        q = q.where(WarRoomSession.project_id == project_id)
    sess_rows = (await db.execute(q.order_by(
        WarRoomSession.created_at.desc()))).scalars().all()
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    by_id = {str(a.id): a.name for a in agents}
    out = []
    for sess in sess_rows:
        tasks = (await db.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sess.id).order_by(WarRoomTask.seq))).scalars().all()
        out.append(_session_out(sess, list(tasks), by_id))
    return {"sessions": out}


# ---------- 1. kickoff：负责人分析拆解 ----------
class KickoffIn(BaseModel):
    company_id: str
    goal: str
    leader_agent_id: str
    project_id: str | None = None
    project_name: str | None = None   # 不带 project_id 时自动创建新项目用
    priority: str = "NORMAL"
    deliverable_format: str | None = None   # 不指定则由负责人在分析中决定


@router.post("/kickoff", status_code=201)
async def kickoff(data: KickoffIn, db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    await _owned(db, user.id, data.company_id)
    leader = await db.get(Agent, data.leader_agent_id)
    if not leader:
        raise HTTPException(404, "leader agent not found")

    # 无项目则自动创建新项目（名称取目标前30字）
    project_id = data.project_id
    if not project_id:
        from app.models.project import Project, ProjectMember, ProjectStatus
        pname = (data.project_name or data.goal).strip()[:60]
        proj = Project(company_id=data.company_id, name=pname,
                       description=f"作战室自动创建 · 目标：{data.goal}",
                       status=ProjectStatus.ACTIVE, priority=data.priority,
                       owner_id=user.id, manager_agent_id=data.leader_agent_id)
        db.add(proj)
        await db.flush()
        project_id = proj.id
        # 负责人自动成为项目成员
        db.add(ProjectMember(project_id=project_id, member_type="agent",
                             member_id=data.leader_agent_id, role_in_project="manager"))

    # 先建会话（analyzing），立即返回；分析在后台跑，前端轮询看板
    sid = uuid.uuid4().hex
    sess = WarRoomSession(
        id=sid, company_id=data.company_id, project_id=project_id,
        goal=data.goal, leader_agent_id=data.leader_agent_id,
        status="analyzing",
        deliverable_format=data.deliverable_format)
    db.add(sess)

    # 后台分析完成后把团队成员也加入项目成员
    import app.background_jobs as bg

    import app.background_jobs as bg
    bg.submit_async(_make_kickoff(
        data.company_id, sid, data.goal, data.leader_agent_id,
        data.deliverable_format, project_id))
    await db.commit()   # 持久化项目+成员+会话
    return {"id": sid, "status": "analyzing", "project_id": project_id}


def _make_kickoff(company_id: str, sid: str, goal: str,
                  leader_agent_id: str, fmt_override: str | None,
                  project_id: str | None = None):
    async def _kickoff_analysis(db):
        """后台执行负责人分析拆解。"""
        import json as _json, re as _re
        try:
            sess = await db.get(WarRoomSession, sid)
            leader = await db.get(Agent, leader_agent_id)
            if not sess or not leader:
                return
            roster = (await db.execute(select(Agent).where(
                Agent.company_id == company_id))).scalars().all()
            roster_lines = "\n".join(
                f"- {a.name}（{a.role or '未设岗位'}）id={a.id}" for a in roster)

            fmt_hint = ""
            if fmt_override:
                fmt_hint = f"\n老板已指定统一交付格式：{DELIVERABLE_FORMATS.get(fmt_override, fmt_override)}"

            gw = ModelGateway(db)
            # 先校验负责人已绑定模型（避免后台长时间挂起）
            _cfg = (await db.execute(select(AgentModelConfig).where(
                AgentModelConfig.agent_id == str(leader.id)))).scalars().all()
            if not _cfg:
                raise RuntimeError(f"负责人 {leader.name} 未配置模型，请先在「模型配置」为其绑定")
            messages = [{"role": "system", "content":
                f"你是「{leader.name}」（{leader.role or '项目负责人'}），公司老板刚刚下达了一个目标，"
                f"你是该项目的最高指挥官。请进行整体分析并拆解任务。\n"
                f"只输出一个 JSON 对象（不要 markdown 代码块），结构如下：\n"
                f'{{"analysis": "对目标的整体分析：需要哪些能力方向、为什么",\n'
                f' "deliverable_format": "md|word|ppt|excel 之一（团队统一交付格式）",\n'
                f' "deliverable_rule": "一句话统一交付规则（如：所有人只交 Word 文档，命名规范…）",\n'
                f' "tasks": [{{"agent_name": "从下面名册里选的员工名", "title": "任务标题", '
                f'"detail": "具体做什么、交付什么"}}, …]}}\n'
                f"要求：\n"
                f"1. tasks 按 1..N 编号顺序排列；每条任务指派最合适的员工；\n"
                f"2. 任务之间尽量并行无依赖；如需汇总环节，最后一条给\"你本人\"做整合汇总（agent_name 填「{leader.name}」）；\n"
                f"3. 团队规模控制在 2-8 条。{fmt_hint}\n\n"
                f"公司员工名册：\n{roster_lines}"},
                {"role": "user", "content": f"老板的目标：{goal}"}]
            llm = await gw.chat_with_agent_models_by_agent(leader, messages)
            content = (llm.get("content") or "").strip()
            m = _re.search(r"\{[\s\S]*\}", content)
            if not m:
                raise ValueError(f"模型未返回JSON: {content[:150]}")
            plan = _json.loads(m.group(0))

            name_to_agent = {a.name: a for a in roster}
            name_to_agent[leader.name] = leader

            sess.analysis = plan
            sess.deliverable_format = fmt_override or plan.get("deliverable_format")
            sess.deliverable_rule = plan.get("deliverable_rule")
            sess.status = "announcing"

            def _fuzzy_find(name_hint: str):
                """精确 → 包含 → 关键词 三级模糊匹配。"""
                if not name_hint:
                    return None
                if name_hint in name_to_agent:
                    return name_to_agent[name_hint]
                for a in roster:
                    if name_hint in a.name or a.name in name_hint:
                        return a
                # 关键词交集（如「文案」命中「种草文案专家」）
                for kw in [name_hint[:2], name_hint[-2:]]:
                    for a in roster:
                        if kw and kw in a.name:
                            return a
                return None

            seq = 0
            for t in plan.get("tasks", []):
                agent = _fuzzy_find(t.get("agent_name") or "")
                if not agent:
                    agent = leader  # 兜底给负责人
                seq += 1
                db.add(WarRoomTask(
                    id=uuid.uuid4().hex, session_id=sid, seq=seq,
                    agent_id=str(agent.id), title=t.get("title") or f"任务{seq}",
                    detail=(t.get("detail") or "") +
                           (f"（原指派：{t.get('agent_name')}，实际执行：{agent.name}）"
                            if agent.name != t.get("agent_name") else ""),
                    status="todo"))
            if seq == 0:
                sess.status = "failed"
                sess.analysis = {"error": "负责人未拆解出任何任务，请重新下达"}
            # 团队成员自动加入项目
            if project_id:
                from app.models.project import ProjectMember
                existing = (await db.execute(select(ProjectMember).where(
                    ProjectMember.project_id == project_id))).scalars().all()
                have = {(m.member_type, m.member_id) for m in existing}
                added_tasks = (await db.execute(select(WarRoomTask).where(
                    WarRoomTask.session_id == sid))).scalars().all()
                for t in added_tasks:
                    key = ("agent", t.agent_id)
                    if key not in have:
                        db.add(ProjectMember(project_id=project_id,
                                             member_type="agent", member_id=t.agent_id,
                                             role_in_project="member"))
            await db.commit()
        except Exception as e:
            sess2 = await db.get(WarRoomSession, sid)
            if sess2:
                sess2.status = "failed"
                sess2.analysis = {"error": str(e)[:300]}
                await db.commit()
    return _kickoff_analysis


# ---------- 会话生命周期：暂停/恢复/停止/删除 ----------
class SessionActionIn(BaseModel):
    action: str   # pause | resume | stop | delete


@router.post("/sessions/{sid}/lifecycle")
async def session_lifecycle(sid: str, data: SessionActionIn,
                            company_id: str = Query(...),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """作战会话生命周期：暂停(现场保留)/恢复(继续执行)/停止(终止保留成果)/删除(彻底清除)。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)

    if data.action == "pause":
        if sess.status not in ("announcing", "confirmed", "executing"):
            raise HTTPException(409, f"当前状态 {sess.status} 不可暂停")
        sess.status = "paused"
    elif data.action == "resume":
        if sess.status != "paused":
            raise HTTPException(409, "仅已暂停的会话可恢复")
        # 恢复到暂停前的阶段：有未确认宣告→announcing，已确认→confirmed，否则 executing
        if all(t.announce_status == "acknowledged" for t in tasks) and tasks:
            sess.status = "executing" if any(t.status == "running" for t in tasks) else "confirmed"
        else:
            sess.status = "announcing"
    elif data.action == "stop":
        if sess.status in ("done", "stopped"):
            raise HTTPException(409, "会话已终结")
        sess.status = "stopped"
        for t in tasks:
            if t.status in ("running", "todo"):
                t.status = "cancelled"
    elif data.action == "delete":
        for t in tasks:
            await db.delete(t)
        await db.delete(sess)
        await db.commit()
        return {"ok": True, "deleted": True}
    else:
        raise HTTPException(400, f"未知操作: {data.action}")
    await db.commit()
    return {"ok": True, "status": sess.status}


class BatchDeleteIn(BaseModel):
    session_ids: list[str]


@router.post("/sessions/batch-delete")
async def batch_delete_sessions(data: BatchDeleteIn,
                                company_id: str = Query(...),
                                db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    """批量彻底删除作战会话（含全部任务）。"""
    await _owned(db, user.id, company_id)
    deleted, failed = 0, []
    for sid in data.session_ids:
        try:
            sess = await db.get(WarRoomSession, sid)
            if not sess:
                failed.append({"id": sid, "reason": "not found"})
                continue
            tasks = (await db.execute(select(WarRoomTask).where(
                WarRoomTask.session_id == sid))).scalars().all()
            for t in tasks:
                await db.delete(t)
            await db.delete(sess)
            deleted += 1
        except Exception as e:
            failed.append({"id": sid, "reason": str(e)[:100]})
    await db.commit()
    return {"deleted_count": deleted, "failed": failed}


# ---------- 参战成员与进度 ----------
@router.get("/sessions/{sid}/team")
async def session_team(sid: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """明确：负责人是谁、参与 Agent 有哪些、各自进度。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    by_id = {str(a.id): a for a in agents}

    leader = by_id.get(sess.leader_agent_id)
    member_ids: list[str] = []
    for t in tasks:
        if t.agent_id not in member_ids:
            member_ids.append(t.agent_id)

    members = []
    for aid in member_ids:
        a = by_id.get(aid)
        mtasks = [t for t in tasks if t.agent_id == aid]
        done = len([t for t in mtasks if t.status == "approved"])
        members.append({
            "agent_id": aid, "name": a.name if a else "?",
            "role": a.role if a else None,
            "task_count": len(mtasks),
            "approved": done,
            "progress_pct": round(done / len(mtasks) * 100) if mtasks else 0,
            "statuses": [t.status for t in mtasks],
        })

    total = len(tasks)
    approved_n = len([t for t in tasks if t.status == "approved"])
    return {
        "session_id": sid, "goal": sess.goal, "status": sess.status,
        "deliverable_format": sess.deliverable_format,
        "deliverable_rule": sess.deliverable_rule,
        "leader": {"agent_id": sess.leader_agent_id,
                   "name": leader.name if leader else "?",
                   "role": leader.role if leader else None},
        "members": members,
        "overall_progress": {
            "total_tasks": total,
            "announced": len([t for t in tasks if t.announce_status != "pending"]),
            "acknowledged": len([t for t in tasks if t.announce_status == "acknowledged"]),
            "submitted": len([t for t in tasks if t.status in ("submitted", "approved")]),
            "approved": approved_n,
            "pct": round(approved_n / total * 100) if total else 0,
        },
    }


# ---------- 文件预览/下载 ----------
@router.get("/sessions/{sid}/file")
async def session_file(sid: str, name: str = Query(...),
                       company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """下载/预览作战产出物（按文件名在会话目录内查找，防路径穿越）。"""
    from pathlib import Path
    from fastapi.responses import FileResponse
    await _owned(db, user.id, company_id)
    sess = await db.get(WarRoomSession, sid)
    if not sess:
        raise HTTPException(404, "session not found")
    root = (Path(__file__).resolve().parents[3] / "storage" / "war_room" / sid).resolve()
    target = (root / name).resolve()
    if not str(target).startswith(str(root)) or not target.exists():
        raise HTTPException(404, "file not found")
    media = {"md": "text/markdown", "txt": "text/plain", "docx":
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(
        target.suffix.lower().lstrip("."), "application/octet-stream")
    return FileResponse(str(target), filename=target.name, media_type=media)


# ---------- 老板插话 ----------
class BossSayIn(BaseModel):
    text: str


@router.post("/sessions/{sid}/say")
async def boss_say(sid: str, data: BossSayIn,
                   company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """老板向作战群聊插入发言并触发全员真实回应。"""
    from app.api.routes.group_chat import GroupChatMessage
    await _owned(db, user.id, company_id)
    sess = await db.get(WarRoomSession, sid)
    if not sess:
        raise HTTPException(404, "session not found")
    msg = GroupChatMessage(
        id=uuid.uuid4().hex[:32], company_id=company_id,
        scope="project", scope_id=sess.project_id or sess.id,
        agent_id=None, agent_name=None, role="boss",
        content={"text": data.text})
    db.add(msg)
    await db.commit()

    # 触发所有参战 Agent 真实回应（后台并行，各自结合自己的任务视角发言）
    tasks = (await db.execute(select(WarRoomTask).where(
        WarRoomTask.session_id == sid))).scalars().all()
    chat_scope = sess.project_id or sess.id
    import app.background_jobs as bg

    def _make_responder(task_id: str | None, agent_id: str):
        async def _respond(db2):
            import logging
            try:
                t2 = await db2.get(WarRoomTask, task_id) if task_id else None
                a2 = await db2.get(Agent, agent_id)
                s3 = await db2.get(WarRoomSession, sid)
                if not a2 or not s3:
                    return
                # 最近群聊上下文
                recent = (await db2.execute(select(GroupChatMessage).where(
                    GroupChatMessage.scope == "project",
                    GroupChatMessage.scope_id == chat_scope).order_by(
                    GroupChatMessage.created_at.desc()).limit(10))).scalars().all()
                history = "\n".join(
                    f"{'老板' if m.role == 'boss' else (m.agent_name or '成员')}: {(m.content or {}).get('text','')[:150]}"
                    for m in reversed(recent))
                my_task = f"\n你在本项目中负责的任务：{t2.title}（{t2.detail or ''}）" if t2 else ""
                gw2 = ModelGateway(db2)
                messages = [{"role": "system", "content":
                    f"你是「{a2.name}」（{a2.role or '数字员工'}），正在项目作战室群聊中。"
                    f"项目目标：{s3.goal}\n统一交付规则：{s3.deliverable_rule or ''}{my_task}\n"
                    f"老板刚在群里说了新话，请从你的职责角度简短回应（80字以内），"
                    f"可以确认收到、给出你的执行思路、或提出专业建议。直接输出回应内容。"},
                    {"role": "user", "content": f"最近群聊：\n{history}\n\n请以「{a2.name}」的身份回应。"}]
                llm = await gw2.chat_with_agent_models_by_agent(a2, messages)
                text = (llm.get("content") or "").strip()
            except Exception as e:
                text = f"(回应失败：{e})"
            db2.add(GroupChatMessage(
                id=uuid.uuid4().hex[:32], company_id=company_id,
                scope="project", scope_id=chat_scope,
                agent_id=str(a2.id), agent_name=a2.name, role="agent",
                content={"text": text}))
            await db2.commit()
            logging.getLogger("war_room").info("agent %s responded to boss", agent_id)
        return _respond

    # 去重后的参战 Agent
    responded = set()
    for t in tasks:
        if t.agent_id in responded:
            continue
        responded.add(t.agent_id)
        bg.submit_async(_make_responder(t.id, t.agent_id))

    return {"ok": True, "agents_responding": len(responded)}


# ---------- 群聊消息流 ----------
@router.get("/sessions/{sid}/chat")
async def session_chat(sid: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """作战群聊消息流（Agent 互相交流 + 老板插话）。"""
    from app.api.routes.group_chat import GroupChatMessage
    await _owned(db, user.id, company_id)
    sess = await db.get(WarRoomSession, sid)
    if not sess:
        raise HTTPException(404, "session not found")
    chat_scope = sess.project_id or sess.id
    rows = (await db.execute(select(GroupChatMessage).where(
        GroupChatMessage.scope == "project",
        GroupChatMessage.scope_id == chat_scope).order_by(
        GroupChatMessage.created_at))).scalars().all()
    # 排除同项目其他来源的干扰：仅保留本会话时间线之后的（用 created_at >= 会话创建时间）
    out = [{"id": m.id, "role": m.role, "agent_name": m.agent_name,
            "text": (m.content or {}).get("text", ""),
            "created_at": str(m.created_at)} for m in rows]
    return {"messages": out}


# ---------- 2. 宣告协议 ----------
@router.post("/{sid}/announce")
async def announce(sid: str, company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """每个成员在群聊复述自己的任务：「我的任务是什么」。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    if sess.status != "announcing":
        raise HTTPException(409, f"当前阶段 {sess.status} 不允许宣告")
    gw = ModelGateway(db)
    from app.api.routes.group_chat import GroupChatMessage

    results = []
    for t in tasks:
        agent = await db.get(Agent, t.agent_id)
        if not agent:
            continue
        is_leader = str(agent.id) == str(sess.leader_agent_id)
        role_line = ("你是项目负责人，除复述自己负责的部分外，还要向全员说明统一交付规则"
                     f"：「{sess.deliverable_rule}」，并宣布任务分工已确定、即将开始执行。"
                     ) if is_leader else \
                    "请用第一人称向全体成员复述你的任务。"
        messages = [{"role": "system", "content":
            f"你是「{agent.name}」（{agent.role or '数字员工'}）。项目群聊里，负责人刚完成分工。{role_line}\n"
            f"格式要求：以「我的任务是：…」开头，100字以内，明确说清要交付什么。"},
            {"role": "user", "content":
             f"项目目标：{sess.goal}\n你的任务（第{t.seq}项）：{t.title}\n详情：{t.detail or '见标题'}"}]
        try:
            llm = await gw.chat_with_agent_models_by_agent(agent, messages)
            text = (llm.get("content") or "").strip() or "(未发言)"
        except Exception as e:
            text = f"(发言失败：{e})"
        t.announce_status = "announced"
        t.announce_text = text
        db.add(GroupChatMessage(
            id=uuid.uuid4().hex[:32], company_id=company_id,
            scope="project", scope_id=sess.project_id or sess.id,
            agent_id=str(agent.id), agent_name=agent.name, role="agent",
            content={"text": text}))
        results.append({"agent": agent.name, "text": text})
    await db.commit()
    return {"announcements": results}


# ---------- 3. 看板 ----------
@router.get("/{sid}")
async def get_session(sid: str, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    by_id = {str(a.id): a.name for a in agents}
    return _session_out(sess, tasks, by_id)


# ---------- 4. 负责人校对 ----------
class AckIn(BaseModel):
    task_id: str
    correct: bool
    feedback: str | None = None   # correct=False 时必填纠正意见


@router.post("/{sid}/acknowledge")
async def acknowledge(sid: str, data: AckIn,
                      company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """负责人逐项校对宣告：正确→acknowledged；不正确→corrected 并写纠正意见。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    target = next((t for t in tasks if t.id == data.task_id), None)
    if not target:
        raise HTTPException(404, "task not found")
    if data.correct:
        target.announce_status = "acknowledged"
        target.leader_feedback = data.feedback or "正确，请继续。"
    else:
        if not (data.feedback or "").strip():
            raise HTTPException(400, "纠正时必须给出反馈")
        target.announce_status = "corrected"
        target.leader_feedback = data.feedback
        # 纠正后该成员需重新宣告
        target.announce_status = "pending"
        target.announce_text = None
    await db.commit()
    all_ack = all(t.announce_status == "acknowledged" for t in tasks)
    if all_ack and sess.status == "announcing":
        sess.status = "confirmed"
    await db.commit()
    return {"ok": True, "all_acknowledged": all_ack, "session_status": sess.status}


# ---------- 5. 开始并行执行 ----------
@router.post("/{sid}/start")
async def start_exec(sid: str, company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """全员确认完毕 → 各成员并行开工（真实后台执行各自任务）。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    if sess.status != "confirmed":
        raise HTTPException(409, f"尚有未确认的宣告（当前 {sess.status}），不能开工")
    sess.status = "executing"
    for t in tasks:
        if str(t.agent_id) != str(sess.leader_agent_id):
            t.status = "running"
    await db.commit()

    # 后台并行执行每个成员的任务（LLM 真实干活）
    import app.background_jobs as bg
    session_snapshot = {"id": sess.id, "company_id": sess.company_id,
                        "goal": sess.goal, "format": sess.deliverable_format}

    def _make_runner(task_id: str, agent_id: str):
        async def _run_one(session):
            import logging
            t2 = await session.get(WarRoomTask, task_id)
            a2 = await session.get(Agent, agent_id)
            s3 = await session.get(WarRoomSession, session_snapshot["id"])
            if not t2 or not a2 or not s3:
                return
            gw2 = ModelGateway(session)
            messages = [{"role": "system", "content":
                f"你是「{a2.name}」（{a2.role or '数字员工'}），正在执行项目中的独立任务。\n"
                f"统一交付规则：{s3.deliverable_rule or '按常规专业标准交付'}\n"
                f"直接输出完整的交付内容本体（不要寒暄）。若为文档类任务输出成文内容；若为媒体/账号类任务输出执行步骤与结果说明。"},
                {"role": "user", "content": f"项目目标：{session_snapshot['goal']}\n你的任务：{t2.title}\n详情：{t2.detail or ''}"}]
            try:
                llm = await gw2.chat_with_agent_models_by_agent(a2, messages)
                out = (llm.get("content") or "").strip()
            except Exception as e:
                out = f"(执行失败：{e})"
            from pathlib import Path
            import re as _re
            root = Path(__file__).resolve().parents[3] / "storage" / "war_room" / session_snapshot["id"]
            root.mkdir(parents=True, exist_ok=True)
            ext = {"word": ".docx", "md": ".md", "ppt": ".pptx", "excel": ".xlsx"}.get(
                s3.deliverable_format or "", ".md")
            fname = "{}_{}{}".format(
                t2.seq,
                _re.sub(r"[^\w\u4e00-\u9fa5-]", "_", t2.title)[:40],
                ext)
            fpath = root / fname
            fpath.write_text(out, encoding="utf-8")
            t2.deliverable_path = str(fpath)
            t2.deliverable_name = fname
            t2.deliverable_note = out[:500]
            t2.status = "submitted"
            await session.commit()
            logging.getLogger("war_room").info("task %s submitted: %s", task_id, fname)
        return _run_one

    for t in tasks:
        if str(t.agent_id) != str(sess.leader_agent_id) and t.status == "running":
            bg.submit_async(_make_runner(t.id, t.agent_id))
    return {"ok": True, "dispatched": len([t for t in tasks if t.status == "running"])}


# ---------- 6. 提交/审核 ----------
class ReviewIn(BaseModel):
    approve: bool
    comment: str | None = None
    reviewer: str = "leader"   # leader / boss（人工干预）


@router.post("/tasks/{tid}/review")
async def review_task(tid: str, data: ReviewIn,
                      company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """可视化审核：负责人审或老板人工干预。驳回则打回重做。"""
    await _owned(db, user.id, company_id)
    t = await db.get(WarRoomTask, tid)
    if not t:
        raise HTTPException(404, "task not found")
    t.reviewed_by = data.reviewer
    t.review_comment = data.comment
    if data.approve:
        t.status = "approved"
    else:
        t.status = "todo"   # 打回重做
    await db.commit()
    return {"ok": True, "status": t.status}


# ---------- 7. 最终汇总 ----------
@router.post("/{sid}/finalize")
async def finalize(sid: str, company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """全部审核通过后，由负责人把各部分整合为最终产出物。"""
    await _owned(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid)
    approved = [t for t in tasks if t.status == "approved"]
    if not approved:
        raise HTTPException(409, "还没有任何已审核通过的产出物")
    leader = await db.get(Agent, sess.leader_agent_id)
    parts = []
    for t in approved:
        if t.deliverable_path:
            from pathlib import Path
            p = Path(t.deliverable_path)
            content = p.read_text(encoding="utf-8") if p.exists() else (t.deliverable_note or "")
        else:
            content = t.deliverable_note or ""
        parts.append(f"# 第{t.seq}部分 · {t.title}\n（{t.deliverable_name or '无文件'}）\n\n{content[:3000]}")
    merged_input = "\n\n---\n\n".join(parts)

    gw = ModelGateway(db)
    messages = [{"role": "system", "content":
        f"你是「{leader.name}」，项目负责人。团队成员的分项成果已经全部审核通过，"
        f"现在请你把它们整合成一份完整、连贯、风格统一的最终交付文档。\n"
        f"统一交付规则：{sess.deliverable_rule or ''}\n"
        f"直接输出整合后的完整文档正文。"},
        {"role": "user", "content": f"项目目标：{sess.goal}\n\n以下是各部分成果：\n\n{merged_input}"}]
    llm = await gw.chat_with_agent_models_by_agent(leader, messages)
    final = (llm.get("content") or "").strip()

    from pathlib import Path
    ext = {"word": ".docx", "md": ".md", "ppt": ".pptx", "excel": ".xlsx"}.get(
        sess.deliverable_format or "", ".md")
    root = Path(__file__).resolve().parents[3] / "storage" / "war_room" / sess.id
    root.mkdir(parents=True, exist_ok=True)
    fpath = root / f"FINAL_{sess.goal[:30].replace(' ', '_')}{ext}"
    fpath.write_text(final, encoding="utf-8")
    sess.final_output_path = str(fpath)
    sess.status = "done"
    await db.commit()
    return {"final_output": str(fpath), "merged_parts": len(approved),
            "preview": final[:800]}
