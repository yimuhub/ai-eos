"""Meeting API (P2-04)."""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Company
from app.models.meeting import (ActionItemStatus, Meeting, MeetingActionItem,
                                MeetingDecision, MeetingMessage,
                                MeetingParticipant, MeetingStatus, MeetingType)
from app.services.event_system import log_event

router = APIRouter(tags=["meetings"])


async def _meeting_owned(db: AsyncSession, user_id: str, mid: str) -> Meeting:
    m = await db.get(Meeting, mid)
    if not m:
        raise HTTPException(404, "Meeting not found")
    c = await db.get(Company, m.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Meeting not found")
    return m


class MeetingCreateIn(BaseModel):
    company_id: str
    project_id: str | None = None
    title: str
    topic: str | None = None
    context_material: str | None = None
    meeting_type: str = "PROJECT_MEETING"
    participant_ids: list[str]
    coordinator_agent_id: str | None = None


@router.post("/meetings", status_code=201)
async def create_meeting(data: MeetingCreateIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    mtype = data.meeting_type  # 支持内置枚举值 + 自定义类型字符串
    if not data.participant_ids:
        raise HTTPException(400, "至少选择一名参会 Agent")

    m = Meeting(company_id=data.company_id, project_id=data.project_id,
                title=data.title, topic=data.topic,
                context_material=data.context_material, meeting_type=mtype,
                status=MeetingStatus.SCHEDULED, created_by=user.id,
                coordinator_agent_id=data.coordinator_agent_id)
    db.add(m)
    await db.flush()
    for aid in data.participant_ids:
        a = await db.get(Agent, aid)
        if not a:
            continue
        db.add(MeetingParticipant(meeting_id=m.id, agent_id=aid))
    await log_event(db, "meeting.created", "meeting", str(m.id), "user", user.id,
                    {"title": data.title})
    await db.commit()
    return {"id": m.id, "title": m.title}


@router.get("/meetings")
async def list_meetings(company_id: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    q = await db.execute(select(Meeting).where(
        Meeting.company_id == company_id).order_by(Meeting.created_at.desc()))
    return [{"id": m.id, "title": m.title, "status": m.status.value,
             "meeting_type": m.meeting_type, "topic": m.topic}
            for m in q.scalars()]


@router.get("/meetings/{mid}")
async def get_meeting(mid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    m = await _meeting_owned(db, user.id, mid)
    msgs_q = await db.execute(select(MeetingMessage).where(
        MeetingMessage.meeting_id == mid).order_by(MeetingMessage.created_at))
    parts_q = await db.execute(select(MeetingParticipant).where(
        MeetingParticipant.meeting_id == mid))
    dec_q = await db.execute(select(MeetingDecision).where(
        MeetingDecision.meeting_id == mid))
    act_q = await db.execute(select(MeetingActionItem).where(
        MeetingActionItem.meeting_id == mid))

    async def agent_name(aid):
        if not aid:
            return None
        a = await db.get(Agent, aid)
        return a.name if a else None

    messages = []
    for msg in msgs_q.scalars():
        speaker = ("我" if msg.speaker_user_id == user.id else "用户") \
            if msg.speaker_type == "user" else \
            (await agent_name(msg.speaker_agent_id) or "系统")
        messages.append({"speaker": speaker, "speaker_type": msg.speaker_type,
                         "content": msg.content, "phase": msg.phase})

    return {
        "id": m.id, "title": m.title, "status": m.status.value,
        "meeting_type": m.meeting_type,
        "topic": m.topic, "minutes": m.minutes, "summary": m.summary,
        "participants": [
            {"agent_id": p.agent_id, "name": await agent_name(p.agent_id),
             "spoke_count": p.spoke_count} for p in parts_q.scalars()],
        "messages": messages,
        "decisions": [{"id": d.id, "content": d.content, "status": d.status.value}
                      for d in dec_q.scalars()],
        "action_items": [
            {"id": i.id, "content": i.content,
             "assignee": await agent_name(i.assignee_agent_id),
             "status": i.status.value, "task_id": i.task_id}
            for i in act_q.scalars()],
    }


def _kick(mmid: str):
    from app.meeting_engine import schedule_next_round
    schedule_next_round(mmid)


@router.post("/meetings/{mid}/start")
async def start_meeting(mid: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    m = await _meeting_owned(db, user.id, mid)
    if m.status != MeetingStatus.SCHEDULED:
        raise HTTPException(409, f"会议状态为 {m.status.value}，不能开始")
    m.status = MeetingStatus.RUNNING
    await log_event(db, "meeting.started", "meeting", str(m.id), "user", user.id, {})
    await db.commit()
    _kick(mid)
    return {"detail": "started"}


@router.post("/meetings/{mid}/pause")
async def pause_meeting(mid: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    m = await _meeting_owned(db, user.id, mid)
    if m.status != MeetingStatus.RUNNING:
        raise HTTPException(409, f"会议状态为 {m.status.value}，不能暂停")
    m.status = MeetingStatus.PAUSED
    await db.commit()
    return {"detail": "paused"}


@router.post("/meetings/{mid}/resume")
async def resume_meeting(mid: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    m = await _meeting_owned(db, user.id, mid)
    if m.status != MeetingStatus.PAUSED:
        raise HTTPException(409, f"会议状态为 {m.status.value}，不能继续")
    m.status = MeetingStatus.RUNNING
    await db.commit()
    _kick(mid)
    return {"detail": "resumed"}


class SayIn(BaseModel):
    content: str


@router.post("/meetings/{mid}/say", status_code=201)
async def user_say(mid: str, data: SayIn, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    """用户实时插话。若会议进行中，Agent 下一轮会看到这条发言。"""
    m = await _meeting_owned(db, user.id, mid)
    db.add(MeetingMessage(meeting_id=mid, speaker_type="user",
                          speaker_user_id=user.id, content=data.content))
    await log_event(db, "meeting.user_message", "meeting", str(m.id),
                    "user", user.id, {})
    await db.commit()
    return {"detail": "ok"}


@router.post("/meetings/{mid}/end")
async def end_meeting(mid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """提前结束：直接进入总结（由后台完成纪要/决策/行动项提取）。"""
    m = await _meeting_owned(db, user.id, mid)
    if m.status == MeetingStatus.COMPLETED:
        raise HTTPException(409, "会议已结束")
    m.status = MeetingStatus.COMPLETED
    # 直接触发 finalize：把 status 置回 RUNNING 让 engine 走 closing 分支
    from app.meeting_engine import run_meeting_turn
    m.status = MeetingStatus.RUNNING
    await db.commit()

    import app.background_jobs as background_jobs

    async def _finalize_now(session):
        from app.core.config import settings
        settings.debug = False
        from app.meeting_engine import _agents_of, _history, _finalize
        meeting = await session.get(Meeting, mid)
        agents = await _agents_of(session, meeting)
        history = await _history(session, meeting.id)
        # 补充总结发言（如尚未有），然后生成纪要
        decisions, actions = await _finalize(session, meeting, history)
        meeting.status = MeetingStatus.COMPLETED
        await session.commit()

    background_jobs.submit_async(_finalize_now)
    return {"detail": "ending, minutes generating"}


@router.post("/meetings/{mid}/action-items/convert")
async def convert_items(mid: str, project_id: str | None = None,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    m = await _meeting_owned(db, user.id, mid)
    if m.status != MeetingStatus.COMPLETED:
        raise HTTPException(409, "会议尚未结束")
    from app.meeting_engine import convert_action_items
    ids = await convert_action_items(db, mid, m.company_id,
                                     project_id or m.project_id, user.id)
    return {"converted_task_ids": ids}


@router.patch("/action-items/{aid}/dismiss", status_code=204)
async def dismiss_item(aid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    item = await db.get(MeetingActionItem, aid)
    if not item:
        raise HTTPException(404, "not found")
    m = await _meeting_owned(db, user.id, str(item.meeting_id))
    item.status = ActionItemStatus.DISMISSED
    await db.commit()


@router.post("/meetings/{mid}/upload", status_code=201)
async def upload_meeting_material(
    mid: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """会议进行中上传材料：追加到会议 context_material 并存储到公司资产库。"""
    m = await _meeting_owned(db, user.id, mid)
    if m.status not in ("RUNNING", "SCHEDULED", "PAUSED"):
        raise HTTPException(409, f"会议状态为 {m.status}，无法上传")
    # 读取内容（限制 10MB）
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "文件过大（最大 10MB）")
    # 存入会议 context_material（追加）
    existing = m.context_material or ""
    new_text = f"\n\n--- 附件：{file.filename} ---\n"
    if len(existing) + len(new_text) < 50000:
        m.context_material = existing + new_text
        await db.commit()
    return {"ok": True, "filename": file.filename, "size": len(raw)}
