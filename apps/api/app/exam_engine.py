"""CEO 周考：独立监督 Agent 出题、CEO 作答、监督 Agent 评分。

## 为什么要一场考试

运行数据只能回答"做成了没有"，回答不了"这位 CEO 现在是什么人格、
遇到权衡时会怎么选、成本纪律是否稳定"。周考用固定的四维题面
（人格 / 成本 / 决策 / 执行）把这部分显性化，并且**由另一个 Agent 评分**，
避免 CEO 自己给自己打分。

## 自动化与人工干预的边界

* 出题、作答、评分、人格结论全自动，每周一 08:50 由调度触发；
* 老板可以逐题改分、改人格结论、写裁决理由——原分与覆盖人全程留痕；
* 模型不可用时不编造分数：考试记为 SKIPPED，宁缺毋滥。

## 与进化的关系

周考是周度深度复盘的输入之一（`self_evolution.collect_metrics` 会读最近一场）。
分数只影响诊断与建议，不会自动改组织、加预算或关闭治理。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.exam import CeoExam
from app.models.token_usage import TokenUsage
from app.self_evolution import (active_playbook, collect_metrics, period_bounds,
                                playbook_block, _extract_json)

logger = get_logger(__name__)

SUPERVISOR_NAME = "CEO 监督官"
SUPERVISOR_ROLE = "CEO 监督与考核"
MAX_QUESTIONS = 9
DIMENSIONS = {
    "personality": "人格与风险姿态",
    "cost": "Token 与成本纪律",
    "decision": "经营决策质量",
    "execution": "执行、验收与复盘",
}
DIMENSION_ORDER = tuple(DIMENSIONS)

SUPERVISOR_SYSTEM = """你是这家公司的「CEO 监督官」，独立于 CEO，直接对老板负责。

你的职责不是夸奖，也不是挑刺，而是用一场短考判断三件事：
1. 这位 CEO 现在表现出什么人格（进取型 / 稳健型 / 保守型 / 摇摆型），
   证据来自他答题时的取舍，而不是自我介绍；
2. 他对 Token、模型成本与并发预算是否有稳定纪律；
3. 他的经营决策是否可解释、可复算，出问题时会不会诚实归因。

规则：
- 出题必须结合本周真实运行数据，问「如果当时换成你会怎么做」，禁止是非题；
- 评分按题面 rubric 给分，答案空泛、回避矛盾的一律扣分；
- 你不执行任何组织、预算或权限动作，只输出判断与建议；
- 只输出 JSON，不要输出 Markdown 代码块以外的任何解释。"""

ANSWER_SYSTEM = """你是公司的 CEO（超级 Agent）。现在接受独立监督官的周考。

要求：
- 按你真实的运行原则与本周数据作答，不写正确的废话；
- 遇到权衡要明确说选哪边、为什么、放弃了什么；
- 成本问题必须给出可执行的数字或规则，而不是「加强管理」；
- 只输出 JSON，不要辩解考试本身是否合理。"""

GRADER_SYSTEM = """你是独立监督官，正在给 CEO 的周考答卷评分。

要求：
- 逐题按 rubric 给 0 到 max_score 的分，并写一句具体理由（引用答卷原文）；
- 人格结论必须由答题证据推出，给出 risk_posture、decision_style、traits；
- 只输出 JSON，不修改题目、不重写答卷、不臆造未出现的细节。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value):
    """指标里可能带 datetime（如上场考试的 week_start），落 JSON 前统一转字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def week_key(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    return period_bounds("weekly", now or _utcnow())


def _fallback_questions() -> list[dict]:
    """模型不可用时仍能出题：题面固定、rubric 明确，评分仍由监督官给。"""
    seeds = [
        ("personality", "本周如果「快」和「稳」只能选一个，你选哪个？"
                        "请用本周的一件真实任务说明你当时的取舍。",
         "明确选择 + 真实任务 + 说明放弃了什么"),
        ("personality", "当老板的目标与你的风险判断冲突时，你会先做什么、"
                        "后做什么？给出你的升级路径。",
         "是否先对齐目标、再给风险选项，而不是直接服从或直接拒绝"),
        ("cost", "给下周的 Token 预算一个数：总额、按场景拆分、"
                 "超支时先砍哪一类调用。",
         "有具体数字、场景拆分和可执行的止损规则"),
        ("cost", "哪些任务不值得用最贵的模型？请给出判断规则和至少两个例子。",
         "规则可复算、例子真实，而不是「看情况」"),
        ("decision", "一条任务连续失败三次，其中两次是同类失败。"
                     "你下一步会怎么决策？",
         "先归因再决定换人/换流程/停损，有明确阈值"),
        ("decision", "交付质量与交付时间冲突时，你如何向老板提出选择？",
         "给出选项、代价与建议，而不是把矛盾推回给老板"),
        ("execution", "你怎么验证一份产出「真的能用」，而不是「看起来完整」？",
         "有验收清单或复算方式，不是主观感觉"),
        ("execution", "本周最值得沉淀成工作流的一次成功或失败是什么？"
                      "下一周如何复用？",
         "具体事件 + 可复用步骤 + 触发条件"),
    ]
    return [{"id": f"q{i}", "dimension": dim, "question": q,
             "rubric": rubric, "max_score": 10}
            for i, (dim, q, rubric) in enumerate(seeds, 1)]


def _normalize_questions(raw: dict | None, fallback: list[dict]) -> list[dict]:
    items = (raw or {}).get("questions")
    out: list[dict] = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()[:1200]
        if not question:
            continue
        dim = str(item.get("dimension") or "").strip()
        if dim not in DIMENSIONS:
            dim = "decision"
        try:
            max_score = int(item.get("max_score") or 10)
        except (TypeError, ValueError):
            max_score = 10
        out.append({
            "id": f"q{len(out) + 1}",
            "dimension": dim,
            "dimension_label": DIMENSIONS[dim],
            "question": question,
            "rubric": str(item.get("rubric") or "").strip()[:800],
            "max_score": max(5, min(20, max_score)),
        })
        if len(out) >= MAX_QUESTIONS:
            break
    return out or fallback


def _normalize_answers(raw: dict | None, questions: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for item in ((raw or {}).get("answers") or []):
        if isinstance(item, dict):
            by_id[str(item.get("id") or "")] = item
    out: list[dict] = []
    for q in questions:
        item = by_id.get(q["id"]) or {}
        text = str(item.get("answer") or "").strip()[:4000]
        out.append({"id": q["id"], "answer": text,
                    "answered": bool(text)})
    return out


def _normalize_grades(raw: dict | None, questions: list[dict],
                      answers: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for item in ((raw or {}).get("grades") or []):
        if isinstance(item, dict):
            by_id[str(item.get("id") or "")] = item
    answer_map = {a["id"]: a for a in answers}
    out: list[dict] = []
    for q in questions:
        item = by_id.get(q["id"]) or {}
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        score = round(max(0.0, min(float(q["max_score"]), score)), 1)
        if not answer_map.get(q["id"], {}).get("answered"):
            score = 0.0
        out.append({
            "id": q["id"],
            "score": score,
            "max_score": q["max_score"],
            "comment": str(item.get("comment") or "").strip()[:600],
        })
    return out


def score_exam(grades: list[dict], questions: list[dict]) -> tuple[dict, float]:
    """维度分与总分都由分数确定性复算，不采用模型自报的总分。"""
    dims: dict[str, dict] = {d: {"earned": 0.0, "max": 0} for d in DIMENSION_ORDER}
    for q, g in zip(questions, grades):
        bucket = dims[q["dimension"]]
        bucket["earned"] += float(g["score"])
        bucket["max"] += int(q["max_score"])
    scores: dict[str, float | None] = {}
    earned_total = max_total = 0.0
    for dim in DIMENSION_ORDER:
        bucket = dims[dim]
        scores[dim] = (round(bucket["earned"] / bucket["max"] * 100, 1)
                       if bucket["max"] else None)
        earned_total += bucket["earned"]
        max_total += bucket["max"]
    overall = round(earned_total / max_total * 100, 1) if max_total else 0.0
    return scores, overall


def grade_letter(overall: float | None) -> str:
    if overall is None:
        return "—"
    if overall >= 90:
        return "A"
    if overall >= 80:
        return "B"
    if overall >= 70:
        return "C"
    if overall >= 60:
        return "D"
    return "E"


def _personality(raw: dict | None, playbook) -> dict:
    item = (raw or {}).get("personality")
    persona = (playbook.persona if playbook is not None else {}) or {}
    if isinstance(item, dict):
        traits = [str(t).strip()[:40] for t in (item.get("traits") or [])
                  if str(t).strip()][:8]
        evidence = [str(e).strip()[:300] for e in (item.get("evidence") or [])
                    if str(e).strip()][:6]
        kind = str(item.get("type") or item.get("label") or "").strip()[:40]
        return {
            "type": kind or "待观察",
            "risk_posture": str(item.get("risk_posture")
                                or persona.get("risk_posture") or "").strip()[:80],
            "decision_style": str(item.get("decision_style")
                                  or persona.get("decision_style") or "").strip()[:80],
            "traits": traits,
            "evidence": evidence,
            "confidence": str(item.get("confidence") or "medium")[:20],
            "source": "supervisor",
        }
    return {
        "type": "待观察",
        "risk_posture": str(persona.get("risk_posture") or "").strip()[:80],
        "decision_style": str(persona.get("decision_style") or "").strip()[:80],
        "traits": [], "evidence": [], "confidence": "low",
        "source": "playbook",
    }


async def ensure_supervisor_agent(db: AsyncSession, company_id: str,
                                  user_id: str | None = None
                                  ) -> tuple[Agent, bool]:
    """找到或创建这家公司的 CEO 监督官；模型绑定继承 CEO，保证可独立评卷。"""
    existing = (await db.execute(
        select(Agent).where(Agent.company_id == company_id,
                            Agent.name == SUPERVISOR_NAME)
        .order_by(Agent.created_at).limit(1))).scalars().first()
    if existing is not None:
        return existing, False

    from app.ceo_engine import pick_coordinator

    ceo = await pick_coordinator(db, company_id)
    agent = Agent(
        id=str(uuid.uuid4()), company_id=company_id,
        name=SUPERVISOR_NAME, role=SUPERVISOR_ROLE,
        description="独立监督 CEO 的周考、评分与人格评估，直接对老板负责",
        system_prompt=SUPERVISOR_SYSTEM,
        status=AgentStatus.ACTIVE, visibility="company",
        is_ceo=False, created_by_user_id=user_id)
    db.add(agent)
    await db.flush()

    configs = (await db.execute(
        select(AgentModelConfig)
        .where(AgentModelConfig.agent_id == str(ceo.id)))).scalars().all()
    for cfg in configs:
        db.add(AgentModelConfig(agent_id=agent.id, role=cfg.role,
                                model_id=cfg.model_id))
    await db.flush()
    logger.info("创建 CEO 监督官 company=%s agent=%s", company_id, agent.id)
    return agent, True


async def _llm_json(db: AsyncSession, agent: Agent, company_id: str,
                    system: str, prompt: str,
                    max_tokens: int = 1600) -> tuple[dict | None, dict]:
    from app.model_gateway import ModelGateway, usage_context

    started = _utcnow()
    try:
        with usage_context(company_id=company_id, agent=str(agent.id),
                           scenario="LEARNING"):
            res = await ModelGateway(db).chat_with_agent_models_by_agent(
                agent,
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}],
                config={"max_tokens": max_tokens})
    except Exception as exc:  # noqa: BLE001 — 模型不可用记为跳过，不编分数
        logger.warning("周考模型调用失败 company=%s agent=%s: %s: %s",
                       company_id, agent.id, type(exc).__name__, exc)
        return None, {"tokens": 0, "cost": 0.0}
    usage = res.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    row = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.agent_id == str(agent.id),
               TokenUsage.scenario == "LEARNING",
               TokenUsage.created_at >= started))).first()
    cost = float(row[1] or 0.0)
    tokens = max(tokens, int(row[0] or 0))
    return _extract_json(res.get("content") or ""), {
        "tokens": tokens, "cost": round(cost, 4)}


def _questions_prompt(metrics: dict, playbook_text: str) -> str:
    import json
    return (
        "下面是这位 CEO 最近一周的真实运行数据与当前运行人格。\n"
        f"运行数据：{json.dumps(_jsonable(metrics), ensure_ascii=False)[:4000]}\n"
        f"当前运行人格：\n{playbook_text or '（尚无版本化人格）'}\n\n"
        f"请出 {MAX_QUESTIONS} 道以内的题，覆盖四个维度："
        "personality（人格与风险姿态）、cost（Token 与成本纪律）、"
        "decision（经营决策质量）、execution（执行、验收与复盘）。\n"
        "输出 JSON：{\"focus\":\"本周考试重点一句话\","
        "\"questions\":[{\"dimension\":\"personality\",\"question\":\"...\","
        "\"rubric\":\"评分要点\",\"max_score\":10}]}"
    )


def _answer_prompt(questions: list[dict], metrics: dict,
                   playbook_text: str) -> str:
    import json
    return (
        "以下是监督官出的周考题。请逐题作答。\n"
        f"题目：{json.dumps(_jsonable(questions), ensure_ascii=False)}\n"
        f"本周数据：{json.dumps(_jsonable(metrics), ensure_ascii=False)[:3000]}\n"
        f"你的运行人格：\n{playbook_text or '（尚无版本化人格）'}\n\n"
        "输出 JSON：{\"answers\":[{\"id\":\"q1\",\"answer\":\"...\"}]}"
    )


def _grade_prompt(questions: list[dict], answers: list[dict],
                  metrics: dict) -> str:
    import json
    return (
        "请按题目 rubric 给 CEO 的答卷逐题评分，并给出人格结论。\n"
        f"题目：{json.dumps(_jsonable(questions), ensure_ascii=False)}\n"
        f"答卷：{json.dumps(_jsonable(answers), ensure_ascii=False)[:8000]}\n"
        f"本周数据：{json.dumps(_jsonable(metrics), ensure_ascii=False)[:3000]}\n\n"
        "输出 JSON：{\"grades\":[{\"id\":\"q1\",\"score\":8,"
        "\"comment\":\"引用答卷的具体理由\"}],"
        "\"personality\":{\"type\":\"进取型\",\"risk_posture\":\"...\","
        "\"decision_style\":\"...\",\"traits\":[\"...\"],"
        "\"evidence\":[\"...\"],\"confidence\":\"high\"},"
        "\"summary\":\"一句话结论\",\"recommendations\":[\"下周改进项\"]}"
    )


def exam_brief(row: CeoExam) -> dict:
    return {
        "id": row.id, "company_id": row.company_id,
        "ceo_agent_id": row.ceo_agent_id,
        "supervisor_agent_id": row.supervisor_agent_id,
        "period_key": row.period_key,
        "week_start": row.week_start.isoformat() if row.week_start else None,
        "week_end": row.week_end.isoformat() if row.week_end else None,
        "status": row.status, "focus": row.focus,
        "questions": row.questions or [], "answers": row.answers or [],
        "grades": row.grades or [], "scores": row.scores or {},
        "personality": row.personality or {},
        "overall": row.overall, "grade": row.grade,
        "summary": row.summary,
        "recommendations": row.recommendations or [],
        "metrics": row.metrics or {}, "override": row.override or {},
        "tokens_used": int(row.tokens_used or 0),
        "cost": round(float(row.cost or 0.0), 4),
        "error": row.error, "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _notify(db: AsyncSession, row: CeoExam) -> None:
    try:
        from app.services.event_system import log_event
        await log_event(db, "ceo.exam", "company", row.company_id,
                        "system", row.supervisor_agent_id,
                        {"exam_id": row.id, "period": row.period_key,
                         "status": row.status, "overall": row.overall,
                         "grade": row.grade,
                         "personality": (row.personality or {}).get("type")},
                        company_id=row.company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("周考留痕失败 exam=%s: %s", row.id, exc)
    try:
        from app.services.notification_service import notify_company_admins
        personality = (row.personality or {}).get("type") or "待观察"
        await notify_company_admins(
            db, company_id=row.company_id, type="ceo.exam",
            title=f"CEO 周考已出分（{row.period_key}）",
            body=(f"{row.grade} 级 / {row.overall} 分；"
                  f"人格判断：{personality}。{row.summary or ''}")[:400],
            link="/evolution")
    except Exception as exc:  # noqa: BLE001
        logger.warning("周考通知失败 exam=%s: %s", row.id, exc)


async def run_exam(db: AsyncSession, company_id: str, *,
                   user_id: str | None = None,
                   source: str = "manual") -> dict:
    """跑一场本周考试；同一 ISO 周幂等。模型不可用时记 SKIPPED。"""
    from app.ceo_engine import CeoError, pick_coordinator

    now = _utcnow()
    week_start, week_end, period = week_key(now)
    existing = (await db.execute(
        select(CeoExam).where(CeoExam.company_id == company_id,
                              CeoExam.period_key == period)
        .limit(1))).scalars().first()
    if existing is not None:
        return {**exam_brief(existing), "reused": True}

    ceo = await pick_coordinator(db, company_id)
    supervisor, _created = await ensure_supervisor_agent(db, company_id, user_id)
    row = CeoExam(
        id=str(uuid.uuid4()), company_id=company_id,
        ceo_agent_id=str(ceo.id), supervisor_agent_id=str(supervisor.id),
        period_key=period, week_start=week_start,
        week_end=week_end, status="PENDING",
        created_by=user_id, source=source)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # 并发触发：另一场已占住本周，直接复用
        await db.rollback()
        existing = (await db.execute(
            select(CeoExam).where(CeoExam.company_id == company_id,
                                  CeoExam.period_key == period)
            .limit(1))).scalars().first()
        if existing is None:
            raise
        return {**exam_brief(existing), "reused": True}

    row_id = row.id
    metrics = await collect_metrics(db, company_id, week_start, week_end)
    playbook = await active_playbook(db, company_id)
    playbook_text = playbook_block(playbook)
    tokens = cost = 0.0

    try:
        q_raw, q_usage = await _llm_json(
            db, supervisor, company_id, SUPERVISOR_SYSTEM,
            _questions_prompt(metrics, playbook_text), max_tokens=1400)
        tokens += q_usage["tokens"]
        cost += q_usage["cost"]
        questions = _normalize_questions(q_raw, _fallback_questions())
        row.questions = questions
        row.focus = str((q_raw or {}).get("focus") or "").strip()[:500] or None
        row.status = "ANSWERING"
        await db.commit()

        a_raw, a_usage = await _llm_json(
            db, ceo, company_id, ANSWER_SYSTEM,
            _answer_prompt(questions, metrics, playbook_text), max_tokens=2400)
        tokens += a_usage["tokens"]
        cost += a_usage["cost"]
        if a_raw is None:
            row.status = "SKIPPED"
            row.error = "CEO 模型不可用或未返回有效答卷，本场不产生分数"
            row.tokens_used = int(tokens)
            row.cost = round(cost, 4)
            await db.commit()
            await db.refresh(row)
            await _notify(db, row)
            return {**exam_brief(row), "reused": False}
        answers = _normalize_answers(a_raw, questions)
        row.answers = answers

        g_raw, g_usage = await _llm_json(
            db, supervisor, company_id, GRADER_SYSTEM,
            _grade_prompt(questions, answers, metrics), max_tokens=2200)
        tokens += g_usage["tokens"]
        cost += g_usage["cost"]
        if g_raw is None:
            row.status = "SKIPPED"
            row.error = "监督官模型不可用，未产生评分"
            row.tokens_used = int(tokens)
            row.cost = round(cost, 4)
            await db.commit()
            await db.refresh(row)
            await _notify(db, row)
            return {**exam_brief(row), "reused": False}

        grades = _normalize_grades(g_raw, questions, answers)
        scores, overall = score_exam(grades, questions)
        row.grades = grades
        row.scores = scores
        row.overall = overall
        row.grade = grade_letter(overall)
        row.personality = _personality(g_raw, playbook)
        row.summary = str(g_raw.get("summary") or "").strip()[:1000] or None
        row.recommendations = [str(r).strip()[:300]
                               for r in (g_raw.get("recommendations") or [])
                               if str(r).strip()][:8]
        row.metrics = _jsonable(metrics)
        row.status = "GRADED"
        row.tokens_used = int(tokens)
        row.cost = round(cost, 4)
        row.error = None
        await db.commit()
        await db.refresh(row)
        await _notify(db, row)
        return {**exam_brief(row), "reused": False}
    except Exception as exc:  # noqa: BLE001 — 考试失败不能拖垮调度循环
        logger.exception("周考执行失败 company=%s: %s", company_id, exc)
        await db.rollback()
        row = await db.get(CeoExam, row_id)
        if row is None:
            raise
        row.status = "FAILED"
        row.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        row.tokens_used = int(tokens)
        row.cost = round(cost, 4)
        await db.commit()
        await db.refresh(row)
        return {**exam_brief(row), "reused": False}


async def override_exam(db: AsyncSession, row: CeoExam, *, user_id: str,
                        grades: list[dict] | None = None,
                        personality: dict | None = None,
                        summary: str | None = None,
                        overall: float | None = None,
                        note: str | None = None) -> dict:
    """老板人工裁决：可改逐题分、人格结论或总分，原值全部留痕。"""
    if row.status not in ("GRADED", "OVERRIDDEN"):
        raise ValueError("这场考试还没有可覆盖的评分")
    prev = {
        "overall": row.overall, "grade": row.grade,
        "personality": row.personality or {}, "scores": row.scores or {},
    }
    questions = row.questions or []
    if grades:
        by_id = {str(g.get("id")): g for g in row.grades or []
                 if isinstance(g, dict)}
        for patch in grades:
            gid = str(patch.get("id") or "")
            if gid not in by_id:
                continue
            target = dict(by_id[gid])
            try:
                target["score"] = round(max(
                    0.0, min(float(target.get("max_score") or 10),
                             float(patch.get("score") or 0))), 1)
            except (TypeError, ValueError):
                continue
            if patch.get("comment") is not None:
                target["comment"] = str(patch["comment"]).strip()[:600]
            by_id[gid] = target
        ordered = [by_id[str(g.get("id"))] for g in row.grades or []
                   if str(g.get("id")) in by_id]
        row.grades = ordered
        scores, computed = score_exam(ordered, questions)
        row.scores = scores
        if overall is None:
            overall = computed
    if personality:
        merged = dict(row.personality or {})
        merged.update({k: v for k, v in personality.items() if v is not None})
        merged["source"] = "owner"
        row.personality = merged
    if summary is not None:
        row.summary = summary.strip()[:1000] or None
    if overall is not None:
        row.overall = round(max(0.0, min(100.0, float(overall))), 1)
        row.grade = grade_letter(row.overall)
    row.override = {
        "by": user_id, "at": _iso(), "note": (note or "")[:1000] or None,
        "previous": prev,
    }
    row.status = "OVERRIDDEN"
    try:
        from app.services.event_system import log_event
        await log_event(db, "ceo.exam_overridden", "company", row.company_id,
                        "user", user_id,
                        {"exam_id": row.id, "note": (note or "")[:500],
                         "previous_overall": prev["overall"],
                         "overall": row.overall},
                        company_id=row.company_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("周考覆盖留痕失败 exam=%s: %s", row.id, exc)
    await db.commit()
    await db.refresh(row)
    return exam_brief(row)


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------

EXAM_SCHEDULES = (
    ("CEO 周考", "ceo_exam_weekly", "weekly",
     {"time": "08:50", "weekday": 0},
     "每周一由独立监督官出题并评分，早于周度深度复盘"),
)


async def ensure_exam_schedules(db: AsyncSession, company_id: str,
                                user_id: str | None) -> dict:
    from app.models.scheduler import (AutomationSchedule, ScheduleStatus,
                                      compute_next_run)

    out: dict[str, dict] = {}
    now = _utcnow()
    for name, action, trigger, cfg, _desc in EXAM_SCHEDULES:
        existing = (await db.execute(
            select(AutomationSchedule).where(
                AutomationSchedule.company_id == company_id,
                AutomationSchedule.action_type == action,
                AutomationSchedule.status != ScheduleStatus.ARCHIVED)
            .limit(1))).scalars().first()
        if existing is not None:
            out[action] = {"schedule_id": existing.id, "created": False}
            continue
        row = AutomationSchedule(
            id=str(uuid.uuid4()), company_id=company_id, name=name,
            trigger_type=trigger, trigger_config=cfg,
            action_type=action, action_params=cfg,
            status=ScheduleStatus.ACTIVE,
            next_run_at=compute_next_run(trigger, cfg, now),
            created_by=user_id)
        db.add(row)
        out[action] = {"schedule_id": row.id, "created": True}
    await db.commit()
    return out
