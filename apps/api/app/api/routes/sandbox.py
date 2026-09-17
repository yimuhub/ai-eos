"""Sandbox & Computer Runtime API (P3-10 + P3-11)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.sandbox import (HIGH_RISK_ACTIONS, MOCK_SCREENSHOT, ComputerSession,
                         ComputerWorkspace, SessionAction, WorkspaceFile,
                         WorkspaceProvider)
from app.models.user import User

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


# ---------- 工作区 ----------

@router.post("/workspaces", status_code=201)
async def create_workspace(
    name: str = Query(...),
    company_id: str = Query(...),
    provider: WorkspaceProvider = Query(WorkspaceProvider.MOCK),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    w = ComputerWorkspace(id=str(__import__("uuid").uuid4()),
                          company_id=company_id, name=name, provider=provider)
    db.add(w)
    await db.commit()
    return {"id": w.id, "name": w.name, "provider": w.provider.value}


@router.get("/workspaces")
async def list_workspaces(company_id: str = Query(...),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    q = await db.execute(select(ComputerWorkspace).where(
        ComputerWorkspace.company_id == company_id))
    return [{"id": w.id, "name": w.name, "provider": w.provider.value}
            for w in q.scalars().all()]


# ---------- 会话 ----------

class SessionIn(BaseModel):
    goal: str | None = None


@router.post("/workspaces/{wid}/sessions", status_code=201)
async def start_session(
    wid: str,
    data: SessionIn | None = None,
    agent_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    w = await db.get(ComputerWorkspace, wid)
    if not w:
        raise HTTPException(404, "workspace not found")
    s = ComputerSession(id=str(__import__("uuid").uuid4()), workspace_id=wid,
                        company_id=w.company_id, agent_id=agent_id,
                        goal=(data.goal if data else None))
    db.add(s)
    await db.commit()
    return {"id": s.id, "status": s.status}


async def _session(db, sid) -> ComputerSession:
    s = await db.get(ComputerSession, sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


def _check_running(s: ComputerSession):
    if s.status == "PAUSED":
        raise HTTPException(409, "会话已暂停，请先恢复")
    if s.status in ("COMPLETED", "CANCELLED", "FAILED"):
        raise HTTPException(409, f"会话已结束（{s.status}）")


@router.post("/sessions/{sid}/pause")
async def pause_session(sid: str, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    s = await _session(db, sid)
    if s.status != "RUNNING":
        raise HTTPException(409, f"status={s.status}")
    s.status = "PAUSED"
    s.snapshot_data = {"paused_at_step": True}
    s.ended_at = None
    await db.commit()
    return {"id": sid, "status": "PAUSED"}


@router.post("/sessions/{sid}/resume")
async def resume_session(sid: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    s = await _session(db, sid)
    if s.status != "PAUSED":
        raise HTTPException(409, f"status={s.status}")
    s.status = "RUNNING"
    await db.commit()
    return {"id": sid, "status": "RUNNING", "resumed_from_checkpoint": True}


@router.post("/sessions/{sid}/cancel")
async def cancel_session(sid: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    from datetime import datetime, timezone
    s = await _session(db, sid)
    if s.status in ("COMPLETED", "CANCELLED"):
        raise HTTPException(409, f"status={s.status}")
    s.status = "CANCELLED"
    s.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"id": sid, "status": "CANCELLED"}


# ---------- Action Loop ----------

class ActionIn(BaseModel):
    action_type: str          # screenshot/click/type/key/scroll/open_file/save_file/delete_file...
    params: dict | None = None


MOCK_OBSERVATION = {
    "screenshot": {"screen_text": "[Mock] 桌面可见窗口：Finder、Terminal",
                   "resolution": "2560x1664"},
}
MOCK_RESULT = {
    "click": {"clicked": True},
    "type": {"typed_chars": "{len}"},
    "key": {"pressed": "{key}"},
    "scroll": {"scrolled": True},
    "open_file": {"opened": "{path}"},
    "save_file": {"saved": "{path}"},
    "delete_file": {"deleted": "{path}"},
    "screenshot": {},
}


@router.post("/sessions/{sid}/act")
async def act(
    sid: str,
    data: ActionIn,
    approved: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """执行一步操作。Observe → Act → Verify，记录 before/after 截图。"""
    s = await _session(db, sid)
    _check_running(s)

    high_risk = data.action_type in HIGH_RISK_ACTIONS
    if high_risk and not approved:
        # 高风险动作需人工批准，不执行
        seq_q = await db.execute(select(SessionAction.seq).where(
            SessionAction.session_id == sid).order_by(
            SessionAction.seq.desc()).limit(1))
        next_seq = (seq_q.scalar() or 0) + 1
        pending = SessionAction(
            id=str(__import__("uuid").uuid4()), session_id=sid, seq=next_seq,
            action_type=data.action_type, params=data.params, high_risk=True,
            result={"pending_approval": True})
        db.add(pending)
        await db.commit()
        return {"requires_approval": True, "action_id": pending.id,
                "reason": f"{data.action_type} 是高风险动作"}

    seq_q = await db.execute(select(SessionAction.seq).where(
        SessionAction.session_id == sid).order_by(
        SessionAction.seq.desc()).limit(1))
    seq = (seq_q.scalar() or 0) + 1

    n = seq
    before = MOCK_SCREENSHOT.format(phase="before", n=n)
    after = MOCK_SCREENSHOT.format(phase="after", n=n)

    observation = MOCK_OBSERVATION.get(data.action_type,
                                       {"screen_state": "[Mock] unchanged"})
    params = data.params or {}
    result_tmpl = MOCK_RESULT.get(data.action_type, {"done": True})
    fmt = dict(params or {})
    fmt.setdefault("len", len(str(params.get("text", ""))) if params else 0)
    result = {}
    for k, v in result_tmpl.items():
        try:
            result[k] = v.format(**fmt) if isinstance(v, str) else v
        except (KeyError, IndexError):
            result[k] = v

    # save_file / delete_file 联动工作区文件
    if data.action_type == "save_file":
        path = params.get("path", "/untitled.txt")
        content = params.get("content", "")
        existing = await db.execute(select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == s.workspace_id,
            WorkspaceFile.path == path))
        wf = existing.scalars().first()
        if wf:
            wf.content = content
            wf.size = len(content)
        else:
            db.add(WorkspaceFile(id=str(__import__("uuid").uuid4()),
                                 workspace_id=s.workspace_id, path=path,
                                 content=content, size=len(content)))
    elif data.action_type == "delete_file":
        path = params.get("path")
        dq = await db.execute(select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == s.workspace_id,
            WorkspaceFile.path == path))
        for wf in dq.scalars().all():
            await db.delete(wf)

    action = SessionAction(
        id=str(__import__("uuid").uuid4()), session_id=sid, seq=seq,
        action_type=data.action_type, params=params,
        observation=observation, result=result, verified=True,
        high_risk=high_risk, approved_by=user.id if high_risk else None,
        before_screenshot=before, after_screenshot=after)
    db.add(action)
    await db.commit()
    return {"seq": seq, "action": data.action_type, "result": result,
            "observation": observation,
            "screenshots": {"before": before, "after": after}}


@router.post("/actions/{aid}/approve")
async def approve_action(aid: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """批准待审批的高风险动作。"""
    a = await db.get(SessionAction, aid)
    if not a or not a.high_risk:
        raise HTTPException(404, "not found")
    a.approved_by = user.id
    a.result = {**(a.result or {}), "approved": True}
    await db.commit()
    return {"id": aid, "approved": True}


@router.get("/sessions/{sid}/actions")
async def list_actions(sid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    q = await db.execute(select(SessionAction).where(
        SessionAction.session_id == sid).order_by(SessionAction.seq))
    return [{"seq": a.seq, "action": a.action_type, "high_risk": a.high_risk,
             "result": a.result, "verified": a.verified,
             "before": a.before_screenshot, "after": a.after_screenshot}
            for a in q.scalars().all()]


@router.get("/workspaces/{wid}/files")
async def list_files(wid: str, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    q = await db.execute(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == wid))
    return [{"path": f.path, "size": f.size, "content": (f.content or "")[:200]}
            for f in q.scalars().all()]
