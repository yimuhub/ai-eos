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
import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.doc_generator import generate_document
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig
from app.models.asset import Asset, AssetType
from app.models.company import Company
from app.models.user import User
from app.models.war_room import WarRoomSession, WarRoomTask
from app.model_gateway import ModelGateway

router = APIRouter(prefix="/war-room", tags=["war-room"])
logger = logging.getLogger("war_room")

DELIVERABLE_FORMATS = {
    "md": "Markdown 文档（.md）",
    "word": "Word 文档（.docx）",
    "ppt": "PPT 演示文稿（.pptx）",
    "excel": "Excel 表格（.xlsx）",
}
# session.deliverable_format → doc_generator 的 doc_type
_FORMAT_ALIAS = {"word": "word", "ppt": "ppt", "excel": "excel", "md": "md"}
_ASSET_TYPE_BY_EXT = {
    ".md": AssetType.DOCUMENT, ".txt": AssetType.DOCUMENT,
    ".docx": AssetType.DOCUMENT, ".pptx": AssetType.DOCUMENT,
    ".xlsx": AssetType.DATA,
}


def _safe_filename(name: str) -> str:
    """把 LLM 产出的标题收敛成安全文件名片段。"""
    return re.sub(r"[^\w\u4e00-\u9fa5-]", "_", name or "")[:40] or "untitled"


def _war_room_dir(sid: str) -> Path:
    # 必须与 Asset 落盘同一个 storage 根（settings.storage_dir）。
    # 硬编码 repo/apps/api/storage 会在配置了 STORAGE_DIR 的部署里另起一棵树，
    # 交付中心按相对路径找不到这些文件。
    from app.api.routes.assets import storage_root
    return storage_root() / "war_room" / sid


async def _render_and_register_file(*, session: AsyncSession, company_id: str,
                                    project_id: str | None, agent_id: str,
                                    title: str, content: str, fmt: str,
                                    session_dir: Path,
                                    fname_hint: str | None = None) -> dict:
    """渲染交付内容为真实文件（Office 走 doc_generator），落盘到会话目录并登记 Asset。

    - word/ppt/excel：doc_generator 先写 storage/artifacts/<agent_id>/，再同盘
      move 到会话目录（文件名不变）；移动失败则保留 artifacts 下的真实路径，
      保证 deliverable_path 永远指向一个能打开的文件。
    - md：纯文本直接写进会话目录。
    返回 {file_path, file_name, size, mime, asset_id}。
    """
    fmt = _FORMAT_ALIAS.get((fmt or "").lower(), "md")
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "md":
        fname = fname_hint or f"{_safe_filename(title)}.md"
        fpath = session_dir / fname
        fpath.write_text(content, encoding="utf-8")
        size, mime = len(content.encode("utf-8")), "text/markdown"
    else:
        gen = generate_document(fmt, title, content, agent_id=agent_id)
        src = Path(gen["file_path"])
        fname, fpath = gen["file_name"], src
        size, mime = gen["size"], gen["mime"]
        try:
            target = session_dir / fname
            if src.resolve() != target.resolve():
                shutil.move(str(src), str(target))
                fpath = target
        except OSError as e:
            # 移动失败（跨盘/权限）不丢弃已渲染文件：保留 artifacts 下的路径
            logger.warning("war_room 交付物移入会话目录失败，保留 %s: %s", src, e)

    fpath = Path(fpath).resolve()
    asset = Asset(
        company_id=company_id, name=fname,
        type=_ASSET_TYPE_BY_EXT.get(fpath.suffix.lower(), AssetType.OTHER),
        path=str(fpath), mime_type=mime, size=size,
        created_by=agent_id, creator_type="agent",
        project_id=project_id, task_id=None, agent_id=agent_id)
    session.add(asset)
    await session.flush()
    return {"file_path": str(fpath), "file_name": fname, "size": size,
            "mime": mime, "asset_id": asset.id}


async def _render_war_room_deliverable(session: AsyncSession, task: WarRoomTask,
                                       agent_id: str, out: str, fmt: str | None,
                                       session_dir: Path) -> dict:
    """把成员 LLM 输出渲染成会话交付物，并回填任务字段（status=submitted）。

    单独抽出便于测试：不依赖后台线程与真实模型。
    """
    war = await session.get(WarRoomSession, task.session_id)
    if not war:
        raise ValueError(f"war room session not found: {task.session_id}")
    title = task.title or f"任务{task.seq}"
    fname_hint = f"{task.seq}_{_safe_filename(title)}.md"
    meta = await _render_and_register_file(
        session=session, company_id=war.company_id, project_id=war.project_id,
        agent_id=agent_id, title=title, content=out or "", fmt=fmt or "md",
        session_dir=session_dir, fname_hint=fname_hint)
    task.deliverable_path = meta["file_path"]
    task.deliverable_name = meta["file_name"]
    task.deliverable_note = (out or "")[:500]
    task.status = "submitted"
    return meta


async def _owned_company(db: AsyncSession, user_id: str, company_id: str,
                 capability: Capability = Capability.VIEW) -> Company:
    """公司级门禁：无角色/能力不足一律 404（不泄露公司是否存在）。

    只读端点用默认 VIEW；作战室的一切变更（拉会、宣告、校对、审核、
    整合）都是日常写（OPERATE）；删会话（单个生命周期 delete / 批量删除）
    不可逆，按 DESTRUCTIVE 收口。
    """
    c = await db.get(Company, company_id)
    if not c:
        raise HTTPException(404, "company not found")
    await assert_company_capability(db, user_id, company_id, capability)
    return c


async def _get_session(db: AsyncSession, sid: str,
                       company_id: str) -> tuple[WarRoomSession, list[WarRoomTask]]:
    sess = await db.get(WarRoomSession, sid)
    # 归属校验必须落在会话本身：调用方只校验了"你拥有 query 里的 company_id"，
    # 不比对 sess.company_id 就能拿自己的公司 id 去读写别家的作战室。
    if not sess or sess.company_id != company_id:
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
            "deliverable_note": t.deliverable_note,
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
    await _owned_company(db, user.id, company_id)
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
    await _owned_company(db, user.id, data.company_id, Capability.OPERATE)
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

    # **先 commit 再投递后台任务**：后台 worker 用的是独立引擎/独立连接，
    # 未提交的行它读不到。原来的"先 submit 后 commit"会让后台在会话行
    # 还不存在时返回，会话就永远卡在 analyzing（前端一直转圈）。
    await db.commit()   # 持久化项目+成员+会话

    # 后台分析完成后把团队成员也加入项目成员
    import app.background_jobs as bg

    bg.submit_async(_make_kickoff(
        data.company_id, sid, data.goal, data.leader_agent_id,
        data.deliverable_format, project_id))
    return {"id": sid, "status": "analyzing", "project_id": project_id}


def _make_kickoff(company_id: str, sid: str, goal: str,
                  leader_agent_id: str, fmt_override: str | None,
                  project_id: str | None = None,
                  auto_run: bool = False):
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
            # 短事务：LLM 调用前先提交，别把读事务/写锁举过整段网络调用
            await db.commit()
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

            # 一句话直达：老板已经用一句指令授权，跳过"逐人宣告 → 负责人校对"
            # 的人工环节，直接确认分工并开工。走的是与 /start 完全相同的
            # _dispatch_members（含批量 CAS），不会绕过防重复投递。
            if auto_run and sess.status == "announcing":
                rows = (await db.execute(select(WarRoomTask).where(
                    WarRoomTask.session_id == sid))).scalars().all()
                for t in rows:
                    t.announce_status = "acknowledged"
                sess.auto_run = True
                # 状态必须是 executing：前端"整合最终交付"只在 executing 渲染，
                # 停在 confirmed 会让直达流程永远拿不到最终文件。
                sess.status = "executing"
                await db.commit()
                try:
                    started = await _dispatch_members(db, sess, rows)
                    logger.info("auto_run 已投递 %d 个成员任务 sid=%s", started, sid)
                except Exception as run_err:
                    logger.error(
                        "auto_run 启动失败 sid=%s: %s", sid, run_err, exc_info=True)
                    sess.status = "failed"
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
    await _owned_company(db, user.id, company_id,
                 Capability.DESTRUCTIVE if data.action == "delete"
                 else Capability.OPERATE)
    sess, tasks = await _get_session(db, sid, company_id)

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
    await _owned_company(db, user.id, company_id, Capability.DESTRUCTIVE)
    deleted, failed = 0, []
    for sid in data.session_ids:
        try:
            sess = await db.get(WarRoomSession, sid)
            if not sess or sess.company_id != company_id:
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
    await _owned_company(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid, company_id)
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
    await _owned_company(db, user.id, company_id)
    sess = await db.get(WarRoomSession, sid)
    if not sess or sess.company_id != company_id:
        raise HTTPException(404, "session not found")
    root = _war_room_dir(sid).resolve()
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
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    sess = await db.get(WarRoomSession, sid)
    if not sess or sess.company_id != company_id:
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
    await _owned_company(db, user.id, company_id)
    sess = await db.get(WarRoomSession, sid)
    if not sess or sess.company_id != company_id:
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


@router.get("/sessions/{sid}/snapshot")
async def session_snapshot(sid: str, company_id: str = Query(...),
                           since_id: str | None = Query(None),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """作战室轮询快照：一次返回 session + team + chat 增量。

    - 归属校验与既有端点一致（`_owned` + 会话存在性）；
    - chat 无 since_id 时只回最近 100 条；带 since_id 时回该消息之后
      （含同一秒的消息，按 id 由前端去重）的增量。
    """
    from app.api.routes.group_chat import GroupChatMessage

    await _owned_company(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid, company_id)
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    session_out = _session_out(sess, tasks, {str(a.id): a.name for a in agents})
    # 复用团队面板的聚合逻辑（同样会做归属校验，保证口径一致）
    team_out = await session_team(sid=sid, company_id=company_id, db=db, user=user)

    chat_scope = sess.project_id or sess.id
    base = select(GroupChatMessage).where(
        GroupChatMessage.scope == "project",
        GroupChatMessage.scope_id == chat_scope)
    incremental = False
    since_row = await db.get(GroupChatMessage, since_id) if since_id else None
    if since_row and since_row.scope == "project" and since_row.scope_id == chat_scope:
        # 用 >= 而不是 >：SQLite 的 created_at 只到秒，同一秒的多条消息
        # 用 > 会漏；重复的那条由前端按 id 去重。
        rows = (await db.execute(
            base.where(GroupChatMessage.created_at >= since_row.created_at)
            .order_by(GroupChatMessage.created_at, GroupChatMessage.id)
            .limit(500))).scalars().all()
        incremental = True
    else:
        rows = (await db.execute(
            base.order_by(GroupChatMessage.created_at.desc(),
                          GroupChatMessage.id.desc())
            .limit(100))).scalars().all()
        rows = list(reversed(rows))

    chat = [{"id": m.id, "role": m.role, "agent_name": m.agent_name,
             "text": (m.content or {}).get("text", ""),
             "created_at": str(m.created_at)} for m in rows]
    return {
        "session": session_out,
        "team": team_out,
        "chat": {
            "messages": chat,
            "incremental": incremental,
            "latest_id": chat[-1]["id"] if chat else (since_id or None),
        },
    }


# ---------- 2. 宣告协议 ----------
@router.post("/{sid}/announce")
async def announce(sid: str, company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """每个成员在群聊复述自己的任务：「我的任务是什么」。"""
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    sess, tasks = await _get_session(db, sid, company_id)
    if sess.status != "announcing":
        raise HTTPException(409, f"当前阶段 {sess.status} 不允许宣告")
    gw = ModelGateway(db)
    from app.api.routes.group_chat import GroupChatMessage

    # 结束只读事务：下面每个 Agent 都要跨网络调 LLM，别把事务/写锁举过调用
    await db.commit()

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
        # 短事务：LLM 调用前 commit（上面 db.get 已开新读事务，先释放），
        # 拿到结果立即落库并 commit，再进入下一位成员——避免跨 N 次 LLM 的写事务。
        await db.commit()
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
        await db.commit()
        results.append({"agent": agent.name, "text": text})
    return {"announcements": results}


# ---------- 3. 看板 ----------
@router.get("/{sid}")
async def get_session(sid: str, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id)
    sess, tasks = await _get_session(db, sid, company_id)
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
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    sess, tasks = await _get_session(db, sid, company_id)
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
def _make_runner(task_id: str, agent_id: str, session_snapshot: dict):
    """构造单个成员任务的执行协程（独立后台连接由调用方提供）。

    模块级而非闭包：测试可以直接驱动它，不必真的投递后台线程。
    """
    async def _run_one(session):
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
            if not out:
                raise ValueError("模型返回空内容")
        except Exception as e:
            # 失败绝不落盘、绝不标 submitted：假成功比失败更伤用户
            t2.status = "failed"
            t2.deliverable_path = None
            t2.deliverable_name = None
            t2.deliverable_note = f"执行失败：{type(e).__name__}: {e}"[:500]
            await session.commit()
            logger.warning("war-room task %s failed: %s: %s", task_id,
                           type(e).__name__, e)
            await _settle_session_after_failure(session, s3.id)
            return

        try:
            await _render_war_room_deliverable(
                session, t2, agent_id, out, s3.deliverable_format,
                _war_room_dir(s3.id))
        except Exception as e:
            # 渲染失败同样不能留下不存在的 deliverable 记录
            t2.status = "failed"
            t2.deliverable_path = None
            t2.deliverable_name = None
            t2.deliverable_note = f"交付物生成失败：{type(e).__name__}: {e}"[:500]
            await session.commit()
            logger.warning("war-room task %s render failed: %s: %s", task_id,
                           type(e).__name__, e)
            await _settle_session_after_failure(session, s3.id)
            return

        await session.commit()
        logger.info("task %s submitted: %s", task_id, t2.deliverable_name)
        await _maybe_auto_finalize(session, s3.id)
    return _run_one


async def _maybe_auto_finalize(session: AsyncSession, sid: str) -> None:
    """一句话直达的收口：全员交付完成 → 自动通过审核 → 自动整合最终文件。

    人工作战流程里"逐条审核 + 点整合交付"是老板的控制点；直达流程里老板
    已经用一句指令授权过一次，再让他点 N 次就是把自动化又还给了人。
    """
    try:
        war = await session.get(WarRoomSession, sid)
        if not war or not war.auto_run or war.status != "executing":
            return
        rows = (await session.execute(select(WarRoomTask).where(
            WarRoomTask.session_id == sid))).scalars().all()
        if not rows or any(t.status in ("todo", "running") for t in rows):
            return
        if any(t.status == "failed" for t in rows):
            return  # 有失败就交回给老板决定重做/人工整合
        submitted = [t for t in rows if t.status == "submitted"]
        if not submitted:
            return
        for t in submitted:
            t.status = "approved"
            t.reviewed_by = t.reviewed_by or "leader"
            t.review_comment = t.review_comment or "一句话直达：自动通过"
        await session.commit()
        await _merge_and_deliver(session, war, rows)
        logger.info("auto_run 已自动整合最终交付物 sid=%s", sid)
    except HTTPException as exc:
        logger.warning("auto_run 自动整合未完成 sid=%s: %s", sid, exc.detail)
        await session.rollback()
    except Exception as exc:
        logger.warning("auto_run 自动整合异常 sid=%s: %s", sid, exc, exc_info=True)
        await session.rollback()


async def _settle_session_after_failure(session, sid: str) -> None:
    """成员任务失败后收敛会话状态。

    没有任何 todo/running 成员任务却仍有 failed，会话就不该继续显示
    「并行执行中」；标 failed 后仍可通过重新开工（todo 任务存在时）恢复。
    收敛失败只记日志，不影响任务本身已写入的失败状态。
    """
    try:
        war = await session.get(WarRoomSession, sid)
        if not war or war.status != "executing":
            return
        rows = (await session.execute(
            select(WarRoomTask.status).where(WarRoomTask.session_id == sid))).all()
        statuses = [r[0] for r in rows]
        if any(st in ("todo", "running") for st in statuses):
            return
        if any(st == "failed" for st in statuses):
            war.status = "failed"
            await session.commit()
            logger.warning("war-room session %s marked failed (all tasks settled)", sid)
    except Exception as e:  # 状态收敛失败不能掩盖任务失败事实
        logger.warning("war-room session %s settle failed: %s", sid, e)


@router.post("/{sid}/start")
async def start_exec(sid: str, company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """全员确认完毕 → 各成员并行开工；也用于「驳回后重做」的增量重跑。

    - confirmed：首次开工，CAS confirmed → executing；
    - executing / failed：不强制重新确认，只把仍是 todo 的成员任务重新拉起
      （审核驳回会把任务打回 todo；全部失败时会话会被标 failed）。
    """
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    sess, tasks = await _get_session(db, sid, company_id)

    member_todo = [t.id for t in tasks
                   if str(t.agent_id) != str(sess.leader_agent_id) and t.status == "todo"]
    if sess.status == "confirmed":
        # CAS：concurrent 双击「开始」只有一次能从 confirmed 翻到 executing，
        # 否则同一个成员任务会被提交两次执行（重复 LLM 调用、重复计费）。
        res = await db.execute(
            update(WarRoomSession)
            .where(WarRoomSession.id == sid, WarRoomSession.status == "confirmed")
            .values(status="executing")
            .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            await db.commit()
            raise HTTPException(409, f"会话已被处理（当前 {sess.status}），不能重复开工")
    elif sess.status in ("executing", "failed"):
        if not member_todo:
            raise HTTPException(409, "没有待执行的任务（全部已提交或已审核）")
        if sess.status == "failed":
            # 条件更新：只有仍是 failed 时才恢复 executing，避免覆盖并发状态
            await db.execute(
                update(WarRoomSession)
                .where(WarRoomSession.id == sid, WarRoomSession.status == "failed")
                .values(status="executing")
                .execution_options(synchronize_session=False))
    else:
        raise HTTPException(409, f"尚未完成分工确认（当前 {sess.status}），不能开工")

    # 只拉起仍是 todo 的成员任务：已提交/已通过的不重复执行。
    dispatched = await _dispatch_members(db, sess, tasks)
    return {"ok": True, "dispatched": dispatched}


async def _dispatch_members(db: AsyncSession, sess: WarRoomSession,
                            tasks: list[WarRoomTask]) -> int:
    """把仍是 todo 的成员任务批量置 running 并投递后台执行。

    用 UPDATE ... RETURNING 拿"真正翻转成功"的 id：并发双击时第二个请求
    的 WHERE status='todo' 匹配不到行，拿不到 id，也就不会重复投递。
    lead 自己的汇总任务不在此处执行（由 finalize 负责）。
    """
    agent_of = {t.id: t.agent_id for t in tasks}
    member_todo = [t.id for t in tasks
                   if str(t.agent_id) != str(sess.leader_agent_id)
                   and t.status == "todo"]
    if not member_todo:
        return 0
    flipped = await db.execute(
        update(WarRoomTask)
        .where(WarRoomTask.id.in_(member_todo), WarRoomTask.status == "todo")
        .values(status="running")
        .returning(WarRoomTask.id)
        .execution_options(synchronize_session=False))
    dispatched = [row[0] for row in flipped.all()]
    # 先落库再投递：后台 worker 用独立连接，看不到未提交的行
    await db.commit()

    import app.background_jobs as bg
    session_snapshot = {"id": sess.id, "company_id": sess.company_id,
                        "goal": sess.goal, "format": sess.deliverable_format}
    full: list[str] = []
    for tid in dispatched:
        if bg.submit_async(_make_runner(tid, agent_of[tid],
                                        session_snapshot)) is bg.QUEUE_FULL:
            full.append(tid)
    if full:
        # worker 池满：把没投出去的退回 todo，否则成员任务永远停在 running，
        # 会话既不会 done 也不会 failed（"并行执行中"卡到用户手动删除）
        await db.execute(
            update(WarRoomTask)
            .where(WarRoomTask.id.in_(full), WarRoomTask.status == "running")
            .values(status="todo")
            .execution_options(synchronize_session=False))
        await db.commit()
        logger.warning("war-room 投递队列已满，退回 %d 个成员任务 sid=%s",
                       len(full), sess.id)
    return len(dispatched) - len(full)


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
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
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
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    sess, tasks = await _get_session(db, sid, company_id)
    if not any(t.status == "approved" for t in tasks):
        raise HTTPException(409, "还没有任何已审核通过的产出物")
    return await _merge_and_deliver(db, sess, tasks)


async def _merge_and_deliver(db: AsyncSession, sess: WarRoomSession,
                             tasks: list[WarRoomTask]) -> dict:
    """把已审核通过的分项成果整合成最终交付物（端点与自动收口共用）。"""
    approved = [t for t in tasks if t.status == "approved"]
    if not approved:
        raise HTTPException(409, "还没有任何已审核通过的产出物")
    leader = await db.get(Agent, sess.leader_agent_id)
    parts = []
    for t in approved:
        content = ""
        if t.deliverable_path:
            p = Path(t.deliverable_path)
            # 二进制 Office 文件不能按 UTF-8 读（会 UnicodeDecodeError）；
            # 这类文件用提交时留的文本摘要拼整合输入。
            if p.suffix.lower() in (".md", ".txt", ".csv", ".json"):
                content = p.read_text(encoding="utf-8") if p.exists() else ""
            if not content:
                content = t.deliverable_note or ""
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
    if not final:
        # 汇总失败不能产出空文件并把会话标成 done
        raise HTTPException(502, "负责人未返回最终文档内容，请重试整合")

    # 最终交付物同样走真实渲染器，而不是把 markdown 塞进 .docx/.pptx/.xlsx
    root = _war_room_dir(sess.id)
    fmt = _FORMAT_ALIAS.get((sess.deliverable_format or "").lower(), "md")
    final_title = f"最终交付-{sess.goal[:30]}"
    gen = await _render_and_register_file(
        session=db, company_id=sess.company_id, project_id=sess.project_id,
        agent_id=str(sess.leader_agent_id), title=final_title, content=final,
        fmt=fmt, session_dir=root,
        fname_hint=f"FINAL_{_safe_filename(sess.goal[:30])}.md")
    sess.final_output_path = gen["file_path"]
    sess.status = "done"
    await db.commit()
    return {"final_output": gen["file_path"], "merged_parts": len(approved),
            "preview": final[:800]}
