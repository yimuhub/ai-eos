"""AI Meeting Engine (P2-04).

Meeting flow: coordinator opens -> each agent speaks in turn (aware of prior
speeches) -> discussion rounds -> decisions proposed/adopted -> action items
extracted. All state persisted; user can watch, interject, pause, end early.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting import (ActionItemStatus, DecisionStatus, Meeting,
                                MeetingActionItem, MeetingDecision,
                                MeetingMessage, MeetingParticipant,
                                MeetingStatus)
from app.models.agent import Agent
from app.services.event_system import log_event


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _agents_of(db: AsyncSession, meeting: Meeting) -> list[Agent]:
    q = await db.execute(
        select(Agent).options(selectinload(Agent.model_configs))
        .join(MeetingParticipant, MeetingParticipant.agent_id == Agent.id)
        .where(MeetingParticipant.meeting_id == meeting.id))
    return list(q.scalars().all())


async def _history(db: AsyncSession, meeting_id: int) -> list[dict]:
    q = await db.execute(select(MeetingMessage).where(
        MeetingMessage.meeting_id == meeting_id).order_by(MeetingMessage.created_at))
    msgs = []
    for m in q.scalars():
        who = f"agent:{m.speaker_name or m.speaker_agent_id}" if m.speaker_type == "agent" else m.speaker_type
        msgs.append({"who": who, "content": (m.content or "")[:800]})
    return msgs


async def _say(db: AsyncSession, meeting: Meeting, content: str,
               speaker_type: str = "system", speaker_agent_id: str | None = None,
               phase: str | None = None):
    # 冗余存储姓名，纪要不再显示 UUID
    speaker_name = speaker_agent_id
    if speaker_type == "agent" and speaker_agent_id:
        a = await db.get(Agent, speaker_agent_id)
        if a:
            speaker_name = a.name
    db.add(MeetingMessage(meeting_id=meeting.id, speaker_type=speaker_type,
                          speaker_agent_id=speaker_agent_id,
                          speaker_name=speaker_name,
                          content=content, phase=phase))
    await db.flush()


async def _chat(db: AsyncSession, agent: Agent, system_prompt: str,
                user_prompt: str) -> str:
    """调用模型网关（主→备→降级链），失败时返回占位发言（会议不中断）。"""
    from app.models.agent import AgentModelConfig
    from app.model_gateway import ModelGateway

    q = await db.execute(select(AgentModelConfig).where(
        AgentModelConfig.agent_id == agent.id))
    rows = q.scalars().all()
    by_role = {}
    for r in rows:
        by_role.setdefault(r.role, []).append(r.model_id)
    model_ids = (by_role.get("primary") or []) + (by_role.get("secondary") or []) + (by_role.get("fallback") or [])
    if not model_ids:
        return f"（{agent.name} 未配置模型，跳过发言）"

    try:
        gw = ModelGateway(db)
        from app.model_gateway import set_usage_context
        set_usage_context(agent=agent, scenario="MEETING",
                          detail={"meeting": True})
        result = await gw.chat_with_agent_models(
            model_ids,
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            config={"max_tokens": 500})
        return (result.get("content") or "").strip()
    except Exception as e:
        return f"（{agent.name} 发言失败：{str(e)[:120]}）"


def _speaker_prompt(agent: Agent, meeting: Meeting, history: list[dict],
                    phase: str) -> tuple[str, str]:
    system = (
        f"你是数字员工「{agent.name}」，岗位：{agent.role or '成员'}。"
        f"{(agent.system_prompt or '')[:600]}\n"
        f"你正在参加一场会议：《{meeting.title}》。"
        f"议题：{meeting.topic or '见材料'}。"
    )
    transcript = "\n".join(f"[{h['who']}] {h['content']}" for h in history[-12:])
    if phase == "opening":
        user = (f"会议材料：{(meeting.context_material or '无')[:1500]}\n\n"
                f"请以你的岗位视角发表开场观点（120字以内），直接说内容。")
    elif phase == "discussion":
        user = ("以下是目前的发言记录：\n" + transcript +
                "\n\n请回应你认同或不认同的观点并给出理由；如不同意请直接提出异议（100字以内）。")
    else:  # closing
        user = ("基于全部讨论：" + transcript +
                "\n\n请总结：1条你认为应形成的决策（格式：DECISION: 内容）；"
                "以及1条需要落实的行动（格式：ACTION: 内容 @负责人描述）（各60字内）。")
    return system, user


async def run_meeting_turn(db: AsyncSession, meeting_id: str) -> dict:
    """推进一个会议轮次（每位参与者发一轮言）。由后台线程调用。

    返回 {"status": ..., "done": bool}
    """
    meeting = await db.get(Meeting, meeting_id)
    if not meeting or meeting.status not in (MeetingStatus.RUNNING,):
        return {"status": "skipped", "done": True}

    # ---- 自愈：会议创建超过 30 分钟仍未完成 → 强制终结，不再无限 RUNNING ----
    created_at = meeting.created_at
    if created_at is not None:
        from datetime import datetime, timedelta
        age = datetime.utcnow() - (created_at if created_at.tzinfo is None
                                   else created_at.replace(tzinfo=None))
        if age > timedelta(minutes=30):
            print(f"[meeting {meeting.id}] TIMEOUT after {age} — force complete")
            decisions, actions = [], []
            try:
                history0 = await _history(db, meeting.id)
                decisions, actions = await _finalize(db, meeting, history0)
            except Exception as e:
                print(f"[meeting {meeting.id}] finalize on timeout failed: {e}")
            meeting.status = MeetingStatus.COMPLETED
            await log_event(db, "meeting.completed_timeout", "meeting",
                            str(meeting.id), "system", None,
                            {"decisions": len(decisions), "actions": len(actions)})
            await db.commit()
            return {"status": "COMPLETED_TIMEOUT", "done": True}

    agents = await _agents_of(db, meeting)
    if not agents:
        meeting.status = MeetingStatus.COMPLETED
        await db.commit()
        return {"status": "no_agents", "done": True}

    # 判断当前进行到哪个阶段：无发言→开场；开场完→讨论；讨论≥1轮→总结
    history = await _history(db, meeting.id)
    agent_speeches = [h for h in history if h["who"].startswith("agent:")]
    n_speeches = len(agent_speeches)
    phase = "opening" if n_speeches < len(agents) else \
            "closing" if n_speeches >= len(agents) * 2 else "discussion"
    print(f"[meeting {meeting.id}] phase={phase} speeches={n_speeches} agents={len(agents)}")

    for agent in agents:
        # 用户中途暂停/取消 → 立即停止本轮
        await db.refresh(meeting)
        if meeting.status != MeetingStatus.RUNNING:
            return {"status": meeting.status.value, "done": False}

        system, user = _speaker_prompt(agent, meeting, history, phase)
        speech = await _chat(db, agent, system, user)
        await _say(db, meeting, speech, "agent", agent.id, phase)
        history.append({"who": f"agent:{agent.name}", "content": speech})

        pq = await db.execute(select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.agent_id == agent.id))
        part = pq.scalars().first()
        if part:
            part.spoke_count += 1
        await db.commit()

    if phase == "closing":
        decisions, actions = await _finalize(db, meeting, history)
        meeting.status = MeetingStatus.COMPLETED
        await log_event(db, "meeting.completed", "meeting", str(meeting.id),
                        "system", None,
                        {"decisions": len(decisions), "actions": len(actions)})
        await db.commit()
        return {"status": "COMPLETED", "done": True}

    await db.commit()
    # 关键：本轮完成但会议未结束 → 自动调度下一轮（否则会议会卡在第一轮）
    schedule_next_round(str(meeting.id))
    return {"status": phase, "done": False}


async def _finalize(db: AsyncSession, meeting: Meeting, history: list[dict]):
    """从总结阶段发言中提取 DECISION / ACTION 条目落库。"""
    transcript = "\n".join(h["content"] for h in history)
    from app.model_gateway import ModelGateway
    from app.models.agent import AgentModelConfig
    # 用 _agents_of 预加载 Agent 及其 model_configs（避免懒加载报错）
    agents = await _agents_of(db, meeting)
    _first_agent = agents[0] if agents else None
    _first_m = await db.get(AgentModelConfig, _first_agent.model_configs[0].id) \
        if _first_agent and _first_agent.model_configs else None
    extract_model_ids = [_first_m.id] if _first_m else []
    if not extract_model_ids:
        raise RuntimeError("no model available for meeting extraction")

    extract_sys = ("你是会议记录员。从会议发言中提取所有决策与行动项，输出JSON："
                   '{"decisions":[{"content":"...","proposed_by":"发言人名"}],'
                   '"actions":[{"content":"...","assignee":"负责人名"}]}'
                   "只输出JSON。")
    try:
        gw = ModelGateway(db)
        from app.model_gateway import set_usage_context
        set_usage_context(agent=agents[0] if agents else None,
                          scenario="MEETING", detail={"meeting_extract": True})
        # 复用 _agents_of 预加载好的 agents 列表
        _first_agent = agents[0] if agents else None
        _first_m = await db.get(AgentModelConfig, _first_agent.model_configs[0].id) \
            if _first_agent and _first_agent.model_configs else None
        extract_model_ids = [_first_m.id] if _first_m else []
        if not extract_model_ids:
            raise RuntimeError("no model available for meeting extraction")
        result = await gw.chat_with_agent_models(
            extract_model_ids,
            [{"role": "system", "content": extract_sys},
             {"role": "user", "content": transcript[:4000]}],
            config={"max_tokens": 800})
        raw = result.get("content") or ""
        import json as _json
        start, end = raw.find("{"), raw.rfind("}")
        data = _json.loads(raw[start:end + 1])
    except Exception:
        # 提取失败时的兜底：从 closing 发言中按 DECISION:/ACTION: 前缀提取
        # 若 Agent 未严格按格式输出，也尝试用关键词匹配（"建议"、"结论"、"行动"等）
        data = {"decisions": [], "actions": []}
        for h in history:
            text = h["content"]
            for line in text.splitlines():
                line = line.strip()
                if line.upper().startswith("DECISION:") or line.upper().startswith("决策："):
                    data["decisions"].append({"content": line.split(":", 1)[-1].strip()[:500],
                                              "proposed_by": h["who"]})
                elif line.upper().startswith("ACTION:") or line.upper().startswith("行动："):
                    data["actions"].append({"content": line.split(":", 1)[-1].strip()[:500],
                                            "assignee": ""})
            # 没带前缀时，把发言整体作为建议决策
            if not data["decisions"] and any(kw in text for kw in ("建议", "结论", "建议优先")):
                data["decisions"].append({"content": text[:500], "proposed_by": h["who"]})
            if not data["actions"] and any(kw in text for kw in ("下一步", "行动", "任务", "落地")):
                data["actions"].append({"content": text[:500], "assignee": h["who"]})

    name_to_agent = {a.name: a.id for a in await _agents_of(db, meeting)}

    decisions = []
    for d in (data.get("decisions") or [])[:5]:
        dec = MeetingDecision(
            meeting_id=meeting.id,
            content=str(d.get("content"))[:2000],
            status=DecisionStatus.ADOPTED,
            proposed_by_agent_id=name_to_agent.get(str(d.get("proposed_by", ""))))
        db.add(dec)
        decisions.append(dec)

    actions = []
    for a in (data.get("actions") or [])[:8]:
        item = MeetingActionItem(
            meeting_id=meeting.id,
            content=str(a.get("content"))[:2000],
            assignee_agent_id=name_to_agent.get(str(a.get("assignee", ""))),
            status=ActionItemStatus.PENDING)
        db.add(item)
        actions.append(item)

    # 会议纪要
    lines = [f"# 会议纪要：《{meeting.title}》", "",
             f"- 类型：{meeting.meeting_type}",
             f"- 时间：{utcnow_iso()[:16]}", "", "## 发言记录"]
    for h in history:
        lines.append(f"- **{h['who']}**：{h['content']}")
    if decisions:
        lines += ["", "## 决策"]
        lines += [f"- ✅ {d.content}" for d in decisions]
    if actions:
        lines += ["", "## 行动项"]
        lines += [f"- 📌 {a.content}（待转任务）" for a in actions]
    meeting.minutes = "\n".join(lines)
    meeting.summary = (decisions[0].content if decisions else "会议完成，无正式决策")

    await _say(db, meeting, "会议结束。纪要与行动项已生成。", "system", None, "closing")
    await db.commit()
    return decisions, actions


async def convert_action_items(db: AsyncSession, meeting_id: str,
                               company_id: str, project_id: str | None,
                               created_by: str):
    """把 PENDING 行动项转换为真实任务。"""
    from app.models.task import Task, TaskPriority, TaskStatus

    q = await db.execute(select(MeetingActionItem).where(
        MeetingActionItem.meeting_id == meeting_id,
        MeetingActionItem.status == ActionItemStatus.PENDING))
    items = q.scalars().all()
    converted = []
    for it in items:
        t = Task(company_id=company_id, project_id=project_id,
                 title=f"[会议行动项]{it.content[:80]}",
                 description=f"来自会议行动项。\n{it.content}",
                 status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                 assigned_agent_id=it.assignee_agent_id, created_by=created_by)
        db.add(t)
        await db.flush()
        it.task_id = t.id
        it.status = ActionItemStatus.CONVERTED
        converted.append(t.id)
    await db.commit()
    return converted


def schedule_next_round(meeting_id: str):
    """后台线程推进下一轮会议发言。"""
    import app.background_jobs as background_jobs

    background_jobs.schedule_kick(meeting_id)
