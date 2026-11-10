"""AI Meeting Engine (P2-04).

Meeting flow: coordinator opens -> each agent speaks in turn (aware of prior
speeches) -> discussion rounds -> decisions proposed/adopted -> action items
extracted. All state persisted; user can watch, interject, pause, end early.
"""
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.meeting import (ActionItemStatus, DecisionStatus, Meeting,
                                MeetingActionItem, MeetingDecision,
                                MeetingMessage, MeetingParticipant,
                                MeetingStatus)
from app.models.agent import Agent
from app.services.event_system import log_event

logger = get_logger(__name__)

# 单轮发言最长 600s（background_jobs.MEETING_TIMEOUT_S），纪要提取再留余量。
# 租约比它长，保证"还在正常跑"的轮次不会被别的 worker 抢走。
MEETING_LEASE_S = 15 * 60

# 纪要生成的幂等占位符：minutes 一旦非空，就不再重复提取/重复建行动项
_FINALIZING = "__FINALIZING__"


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
        from app.model_gateway import usage_context
        with usage_context(agent=agent, scenario="MEETING",
                           detail={"meeting": True}):
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
    from app.core.prompting import staff_identity
    system = (
        f"你是数字员工「{agent.name}」，岗位：{agent.role or '成员'}。"
        f"{staff_identity(agent.name)}"
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

    **互斥（租约）**：同一场会议同一时刻只允许一个 worker 推进。此前后台
    调度重复投递（多次 schedule_kick / 超时重排 / 重启恢复）会让两个 worker
    同时跑同一轮 → 同一 Agent 发两次言、纪要生成两份、行动项建两批任务。
    租约落在 runtime_locks（键 meeting:<id>，15 分钟自动过期），进程崩溃
    不会永久死锁：下一轮调度可以抢占过期租约继续推进。

    返回 {"status": ..., "done": bool}
    """
    from app.task_engine import acquire_lock, new_lock_owner, release_lock

    lock_key = f"meeting:{meeting_id}"
    owner = new_lock_owner()
    if not await acquire_lock(db, lock_key, owner, ttl_seconds=MEETING_LEASE_S):
        logger.info("[meeting %s] 本轮已有 worker 在推进，跳过重复调度", meeting_id)
        return {"status": "busy", "done": False}
    result: dict = {}
    try:
        result = await _run_turn_locked(db, meeting_id)
    except Exception as turn_err:
        # 修复：本轮异常此前被吞掉——need_next 恒 False，不再有下一轮调度，
        # 而超时自愈逻辑在 _run_turn_locked 内部（不会再被触发），会议永远
        # 停在 RUNNING。现在按"超时自愈"同款路径收尾：抢占结束权 → 生成
        # 纪要/行动项（尽力而为），会议不再卡死；错误进日志与事件流。
        logger.error("[meeting %s] 轮次异常，走自愈收尾: %s: %s",
                     meeting_id, type(turn_err).__name__, turn_err, exc_info=True)
        meeting = await db.get(Meeting, meeting_id)
        if meeting is not None and meeting.status == MeetingStatus.RUNNING:
            try:
                await _finish_meeting(db, meeting, mode="timeout")
            except Exception as finish_err:
                logger.error("[meeting %s] 异常自愈收尾失败: %s",
                             meeting_id, finish_err, exc_info=True)
    finally:
        # 先释放租约再调度下一轮：否则下一轮 worker 立刻抢锁会拿到 busy，
        # 会议就停在这里了。
        need_next = bool(result.pop("_schedule_next", False)) if result else False
        try:
            await release_lock(db, lock_key, owner)
        except Exception as lease_err:  # 释放失败最多等租约自然过期
            logger.warning("[meeting %s] 释放轮次租约失败: %s", meeting_id, lease_err)
        # 关键：本轮完成但会议未结束 → 自动调度下一轮（否则会议会卡在第一轮）
        if need_next:
            schedule_next_round(meeting_id)
    return result


async def _run_turn_locked(db: AsyncSession, meeting_id: str) -> dict:
    """持租约执行一轮发言（调用方已保证互斥）。"""
    meeting = await db.get(Meeting, meeting_id)
    if not meeting or meeting.status not in (MeetingStatus.RUNNING,):
        return {"status": "skipped", "done": True}

    # ---- 自愈：会议创建超过 30 分钟仍未完成 → 强制终结，不再无限 RUNNING ----
    created_at = meeting.created_at
    if created_at is not None:
        from datetime import datetime as _dt, timedelta
        age = _dt.utcnow() - (created_at if created_at.tzinfo is None
                              else created_at.replace(tzinfo=None))
        if age > timedelta(minutes=30):
            logger.warning("[meeting %s] TIMEOUT after %s — force complete",
                           meeting.id, age)
            finished = await _finish_meeting(db, meeting, mode="timeout")
            return {"status": "COMPLETED_TIMEOUT" if finished else "skipped",
                    "done": True}

    agents = await _agents_of(db, meeting)
    if not agents:
        # CAS：只有仍 RUNNING 才落 COMPLETED，不覆盖用户刚点的取消
        res = await db.execute(update(Meeting).where(
            Meeting.id == meeting.id,
            Meeting.status == MeetingStatus.RUNNING).values(
            status=MeetingStatus.COMPLETED)
            .execution_options(synchronize_session=False))
        await db.commit()
        return {"status": "no_agents", "done": True}

    # 判断当前进行到哪个阶段：无发言→开场；开场完→讨论；讨论≥1轮→总结
    history = await _history(db, meeting.id)
    agent_speeches = [h for h in history if h["who"].startswith("agent:")]
    n_speeches = len(agent_speeches)
    phase = "opening" if n_speeches < len(agents) else \
            "closing" if n_speeches >= len(agents) * 2 else "discussion"
    logger.info("[meeting %s] phase=%s speeches=%s agents=%s",
                meeting.id, phase, n_speeches, len(agents))
    # 结束只读事务：下面要对每个 Agent 调 LLM，别把事务举过网络调用
    await db.commit()

    for agent in agents:
        # 用户中途暂停/取消/结束 → 立即停止本轮（每步前重读状态）
        await db.refresh(meeting)
        if meeting.status != MeetingStatus.RUNNING:
            return {"status": meeting.status.value, "done": False}

        system, user = _speaker_prompt(agent, meeting, history, phase)
        # 短事务：LLM 调用前 commit（释放读事务/写锁），拿到结果再开新事务落库
        await db.commit()
        speech = await _chat(db, agent, system, user)
        # 写发言 + 当前发言人（同时是租约心跳：updated_at 随行更新）
        meeting.current_speaker_id = agent.id
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
        await _finish_meeting(db, meeting, mode="normal")
        return {"status": "COMPLETED", "done": True}

    return {"status": phase, "done": False, "_schedule_next": True}


async def _finish_meeting(db: AsyncSession, meeting: Meeting,
                          mode: str = "normal") -> bool:
    """抢占"结束权"并把会议收尾（生成纪要/决策/行动项）。

    只有把 RUNNING 改成 COMPLETED 的那一个 worker 继续收尾：超时自愈、
    正常收尾、用户「结束会议」三条路径可能同时到达，原实现会各生成一份
    纪要、各建一批行动项。先 commit 结束状态（短事务），再花时间调 LLM。
    """
    res = await db.execute(
        update(Meeting)
        .where(Meeting.id == meeting.id,
               Meeting.status == MeetingStatus.RUNNING)
        .values(status=MeetingStatus.COMPLETED)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        return False
    set_committed_value(meeting, "status", MeetingStatus.COMPLETED)

    history = await _history(db, meeting.id)
    decisions, actions = [], []
    try:
        decisions, actions = await _finalize(db, meeting, history)
    except Exception as e:
        logger.error("[meeting %s] finalize failed: %s: %s",
                     meeting.id, type(e).__name__, e, exc_info=True)
    await log_event(db, "meeting.completed_timeout" if mode == "timeout"
                    else "meeting.completed", "meeting",
                    str(meeting.id), "system", None,
                    {"decisions": len(decisions), "actions": len(actions)})
    await db.commit()
    return True


async def _finalize(db: AsyncSession, meeting: Meeting, history: list[dict]):
    """从总结阶段发言中提取 DECISION / ACTION 条目落库。

    幂等：minutes 是"纪要已生成"的唯一标记。并发的第二次调用（超时自愈 +
    正常收尾 + 用户结束会议同时到达）在这里被 CAS 挡住，只返回已有结果，
    不再重复提取、不再重复建行动项。
    """
    res = await db.execute(
        update(Meeting)
        .where(Meeting.id == meeting.id, Meeting.minutes.is_(None))
        .values(minutes=_FINALIZING)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        existing_dec = (await db.execute(select(MeetingDecision).where(
            MeetingDecision.meeting_id == meeting.id))).scalars().all()
        existing_act = (await db.execute(select(MeetingActionItem).where(
            MeetingActionItem.meeting_id == meeting.id))).scalars().all()
        logger.info("[meeting %s] 纪要已生成，跳过重复提取", meeting.id)
        return list(existing_dec), list(existing_act)
    set_committed_value(meeting, "minutes", _FINALIZING)

    transcript = "\n".join(h["content"] for h in history)
    from app.model_gateway import ModelGateway
    # _agents_of 已 selectinload 预加载 Agent.model_configs，直接取
    # 第一条配置行的 model_id（此前误把配置行主键 AgentModelConfig.id
    # 当模型 id 传给网关 → db.get(Model, mid) 必然落空，LLM 纪要实为死代码）
    agents = await _agents_of(db, meeting)
    extract_model_ids: list[str] = []
    if agents and agents[0].model_configs:
        mid = agents[0].model_configs[0].model_id
        if mid:
            extract_model_ids = [str(mid)]

    extract_sys = ("你是会议记录员。从会议发言中提取所有决策与行动项，输出JSON："
                   '{"decisions":[{"content":"...","proposed_by":"发言人名"}],'
                   '"actions":[{"content":"...","assignee":"负责人名"}]}'
                   "只输出JSON。")
    data = None
    if extract_model_ids:
        try:
            gw = ModelGateway(db)
            from app.model_gateway import usage_context
            with usage_context(agent=agents[0] if agents else None,
                               scenario="MEETING",
                               detail={"meeting_extract": True}):
                result = await gw.chat_with_agent_models(
                    extract_model_ids,
                    [{"role": "system", "content": extract_sys},
                     {"role": "user", "content": transcript[:4000]}],
                    config={"max_tokens": 800})
            raw = result.get("content") or ""
            import json as _json
            start, end = raw.find("{"), raw.rfind("}")
            data = _json.loads(raw[start:end + 1])
        except Exception as e:
            # 降级不再静默：用户以为拿到的是 LLM 纪要，必须留痕说明实际是兜底结果
            logger.warning("会议 %s LLM 纪要提取失败，降级为关键词匹配（纪要可能不完整）：%s: %s",
                           meeting.id, type(e).__name__, e, exc_info=True)
    else:
        logger.warning("会议 %s 无可用提取模型（首位参会者未绑定模型配置），"
                       "降级为关键词匹配（纪要可能不完整）", meeting.id)
    if data is None:
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
             # 正文是给人看的：写服务器本地时间，避免和页面其余时刻差 8 小时
             f"- 时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
             "", "## 发言记录"]
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
    # 纪要写库与通知同事务提交：只有真正拿到"纪要生成权"的 worker 会走到这里，
    # 幂等占位符（_FINALIZING）已挡住并发重复提取，因此不会重复通知。
    from app.services.notification_service import notify_meeting_minutes
    await notify_meeting_minutes(db, meeting)
    await db.commit()
    return decisions, actions


async def convert_action_items(db: AsyncSession, meeting_id: str,
                               company_id: str, project_id: str | None,
                               created_by: str):
    """把 PENDING 行动项转换为真实任务。

    并发/重复点击「转任务」时，每条行动项用 CAS（PENDING→CONVERTED）领取：
    只有一个请求会为它建任务，其余请求跳过 —— 否则同一份行动项会建出
    两批一模一样的任务。
    """
    from app.models.task import Task, TaskPriority, TaskStatus

    q = await db.execute(select(MeetingActionItem).where(
        MeetingActionItem.meeting_id == meeting_id,
        MeetingActionItem.status == ActionItemStatus.PENDING))
    items = q.scalars().all()
    converted = []
    for it in items:
        res = await db.execute(
            update(MeetingActionItem)
            .where(MeetingActionItem.id == it.id,
                   MeetingActionItem.status == ActionItemStatus.PENDING)
            .values(status=ActionItemStatus.CONVERTED)
            .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            continue  # 已被别的请求领走
        set_committed_value(it, "status", ActionItemStatus.CONVERTED)
        t = Task(company_id=company_id, project_id=project_id,
                 title=f"[会议行动项]{it.content[:80]}",
                 description=f"来自会议行动项。\n{it.content}",
                 status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                 assigned_agent_id=it.assignee_agent_id, created_by=created_by)
        db.add(t)
        await db.flush()
        it.task_id = t.id
        converted.append(t.id)
    await db.commit()
    return converted


def schedule_next_round(meeting_id: str):
    """后台线程推进下一轮会议发言。"""
    import app.background_jobs as background_jobs

    background_jobs.schedule_kick(meeting_id)
