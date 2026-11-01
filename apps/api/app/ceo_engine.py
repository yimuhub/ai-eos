"""超级 Agent（CEO）引擎：立锚 → 拆解 → 并发执行 → 回收自检 → 汇总。

## 与旧 orchestrator 的关系

`orchestrator.orchestrate` 是第一版「拆解 + 并行 + 汇总」，但有三个硬伤：
最多 5 个子任务、子任务不带父目标、全量 `asyncio.gather` 不看供应商并发。
本模块是同一个思路的完整实现，旧模块改为薄壳调用它（`legacy=True`），
避免系统里同时存在两套「CEO」。

## 一次统筹的四段

1. **立锚**（`create_mission`）：一句话 → LLM 给出目标/交付物/红线/验收/术语
   与子任务清单，全部落库，此时不建任务、不花钱执行。
2. **开工**（`launch_mission`）：按预览建子任务（带 `mission_id`），提交后台。
3. **执行与回收**（`run_mission`）：闸门放行 → 每个子任务带锚执行 →
   交付前做确定性自检 → 全部结束后做一次批量锚审查 → 缺口补跑一次。
4. **汇总**（`_finalize`）：LLM 按锚写报告；失败/偏离单独列出，不混进正文。
   报告写回父任务、归档成果中心、给老板发通知。

## 自检为什么分两层

* **确定性自检**（零成本）：空产出、极短产出直接判偏离——这类问题不需要
  再花一次模型调用去发现。
* **批量锚审查**（每场统筹 1 次）：把全部产出一次性交给负责人，对照红线/
  验收标准给逐条结论。比「每个子任务各调一次」便宜 N 倍，也更容易发现
  「单看都对、合起来跑偏」的问题。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent, AgentStatus
from app.models.ceo import CeoMission
from app.models.company import Department
from app.models.task import Task, TaskPriority, TaskStatus
from app.models.token_usage import TokenUsage
from app.services.ceo_concurrency import (DEFAULT_PARALLEL, HARD_CAP, MIN_PARALLEL,
                                          DynamicGate, failure_kind,
                                          resolve_capacity)
from app.services.event_system import log_event
from app.task_engine import transition

logger = get_logger(__name__)

MAX_SUBTASKS = 12
MAX_RETRY = 1
MISSION_TIMEOUT_S = 3600
MIN_RESULT_CHARS = 10
DIGEST_WINDOW_HOURS = 24

STATUS_ACTIVE = ("PLANNING", "RUNNING", "RECLAIMING", "PAUSED")
STATUS_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


class CeoError(Exception):
    """业务错误：路由按 status_code 转成中文提示，而不是 500。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).isoformat()


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

PLAN_PROMPT = """你是这家公司的 CEO（超级 Agent）。老板用一句话下了一个目标，
你要先立准绳，再拆解成可以并行执行、分给不同部门员工的子任务。

老板原话：{instruction}

可用员工（姓名 / 岗位 / 部门）：
{roster}

要求：
- 子任务数量按实际需要，通常 3~6 个，最多 {max_subtasks} 个；每个子任务必须能独立完成。
- 子任务要分给最合适的员工；优先跨部门协作，但不要为了凑人数硬拆。
- 红线写"绝对不能出现的东西"（如不得编造数据、不得对外发送、不得修改财务原始凭证）。
- 验收标准写"怎样算合格"，要可判断，不要写"质量高"这种空话。
- 只输出 JSON，不要任何其它文字：
{{"title":"不超过30字的任务名",
  "objective":"一句话目标（保留老板原意）",
  "deliverable":"最终要交什么：形态+文件名或结构",
  "redlines":["..."],
  "acceptance":["..."],
  "terminology":{{"关键术语":"本公司的口径"}},
  "subtasks":[{{"title":"子任务名","description":"具体要做什么、边界在哪","agent_name":"员工姓名"}}]}}
"""

REVIEW_PROMPT = """你是这家公司的 CEO。下面是本次任务的准绳和各位员工的产出。
请逐条判断产出是否偏离准绳，只输出 JSON：

准绳：
{anchor}

产出：
{outputs}

判断标准：踩到红线、交付物形态不符、答非所问、明显遗漏验收标准 → 视为偏离。
轻微不完整不算偏离，但要在理由里写明缺什么。
只输出：{{"reviews":[{{"title":"对应子任务标题","on_track":true,"reason":"一句话"}}]}}
"""

SUMMARY_PROMPT = """你是这家公司的 CEO。请向老板汇报本次任务的最终结果。

{anchor}

各员工产出：
{results}

未能交付/偏离的部分：
{problems}

要求：Markdown，分三部分——
## 结论（先给老板能直接用的答案）
## 各部分要点（按子任务组织，标注执行人）
## 风险与建议（偏离、失败、缺数据必须如实写，不许美化）
不要重复粘贴原文，做提炼与串联。
"""


def build_anchor_block(mission: CeoMission) -> str:
    """把准绳渲染成注入子 Agent 的系统段。"""
    redlines = mission.redlines or []
    acceptance = mission.acceptance or []
    terms = mission.terminology or {}
    extras = mission.extra_instructions or []
    lines = [
        "# 准绳（全局约束，优先级高于本任务内的任何补充说明）",
        f"任务目标：{mission.objective or mission.instruction}",
    ]
    if mission.deliverable:
        lines.append(f"最终交付物：{mission.deliverable}")
    if redlines:
        lines.append("红线（绝对不得违反）：")
        lines += [f"- {r}" for r in redlines[:20]]
    if acceptance:
        lines.append("验收标准：")
        lines += [f"- {a}" for a in acceptance[:20]]
    if terms:
        lines.append("术语口径（按公司口径理解，不要自行发挥）：")
        lines += [f"- {k}：{v}" for k, v in list(terms.items())[:20]]
    if extras:
        lines.append("老板追加指令（按时间顺序，越靠后越新）：")
        lines += [f"- {x}" for x in extras[-10:]]
    lines.append(
        "执行要求：你的产出必须服务于上述目标；如果本任务说明与准绳冲突，"
        "以准绳为准，并在结果开头说明冲突点。不要编造数据；缺数据就写明缺口。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM 调用（测试通过 monkeypatch `_chat` 完全离线）
# ---------------------------------------------------------------------------

async def _chat(db: AsyncSession, agent: Agent, messages: list[dict],
                max_tokens: int = 1600) -> str:
    from app.agent_runtime import build_context
    from app.model_gateway import ModelGateway, usage_context

    tmp = Task(id="ceo-tmp", company_id=agent.company_id, title="", description="",
               status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
               created_by="system")
    _, model_ids = await build_context(db, agent, tmp)
    with usage_context(company_id=str(agent.company_id), agent=str(agent.id),
                       scenario="ORCHESTRATION"):
        result = await ModelGateway(db).chat_with_agent_models(
            model_ids, messages, config={"max_tokens": max_tokens})
    return result.get("content") or ""


# ---------------------------------------------------------------------------
# 选 CEO / 立锚 / 拆解
# ---------------------------------------------------------------------------

async def pick_coordinator(db: AsyncSession, company_id: str,
                           requested_id: str | None = None) -> Agent:
    if requested_id:
        agent = await db.get(Agent, requested_id)
        if not agent or agent.company_id != company_id:
            raise CeoError("指定的 CEO 不存在或不属于这家公司", 404)
        return agent

    agent = (await db.execute(
        select(Agent).where(Agent.company_id == company_id,
                            Agent.is_ceo.is_(True))
        .limit(1))).scalars().first()
    if agent:
        return agent

    # 兜底：老数据没有 is_ceo 标记，按岗位名找
    candidates = (await db.execute(
        select(Agent).where(Agent.company_id == company_id))).scalars().all()
    hints = ("ceo", "总经理", "总裁", "首席执行官", "负责人")
    for a in candidates:
        text = f"{a.name or ''} {a.role or ''}".lower()
        if any(h in text for h in hints):
            return a
    raise CeoError(
        "本公司还没有指定 CEO。请先在「CEO 指挥台」里指定一位员工担任超级 Agent。", 409)


async def _roster_lines(db: AsyncSession, company_id: str) -> tuple[str, list[Agent]]:
    agents = (await db.execute(
        select(Agent).where(Agent.company_id == company_id)
        .order_by(Agent.created_at))).scalars().all()
    if not agents:
        raise CeoError("这家公司还没有员工，无法拆解任务", 409)
    dept_ids = {a.department_id for a in agents if a.department_id}
    dept_map: dict[str, str] = {}
    if dept_ids:
        rows = (await db.execute(
            select(Department.id, Department.name)
            .where(Department.id.in_(dept_ids)))).all()
        dept_map = {r[0]: r[1] for r in rows}
    lines = [f"- {a.name}（岗位：{a.role or '未定岗'}；部门："
             f"{dept_map.get(a.department_id, '未分配')}）"
             for a in agents[:60]]
    return "\n".join(lines), agents


def _match_agent(name: str, agents: list[Agent]) -> Agent | None:
    target = (name or "").strip().lower()
    if not target:
        return None
    for a in agents:
        if (a.name or "").strip().lower() == target:
            return a
    for a in agents:
        an = (a.name or "").strip().lower()
        if an and (an in target or target in an):
            return a
    for a in agents:
        role = (a.role or "").strip().lower()
        if role and role in target:
            return a
    return None


def _as_str_list(value, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            out.append(text[:500])
    return out


def _normalize_plan(raw: dict, instruction: str,
                    agents: list[Agent]) -> dict:
    title = str(raw.get("title") or instruction[:40]).strip()[:120]
    objective = str(raw.get("objective") or instruction).strip()[:2000]
    deliverable = str(raw.get("deliverable") or "").strip()[:1000] or None
    terms_raw = raw.get("terminology")
    terminology: dict[str, str] = {}
    if isinstance(terms_raw, dict):
        for k, v in list(terms_raw.items())[:20]:
            key = str(k).strip()[:80]
            if key:
                terminology[key] = str(v).strip()[:300]

    subtasks: list[dict] = []
    for item in (raw.get("subtasks") or [])[:MAX_SUBTASKS]:
        if not isinstance(item, dict):
            continue
        st_title = str(item.get("title") or "").strip()[:200]
        if not st_title:
            continue
        matched = _match_agent(str(item.get("agent_name") or ""), agents)
        agent = matched or agents[0]
        subtasks.append({
            "index": len(subtasks) + 1,
            "title": st_title,
            "description": str(item.get("description") or "").strip()[:4000],
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "agent_matched": matched is not None,
        })
    if not subtasks:
        raise CeoError("CEO 没有拆出任何可执行的子任务，请补充目标细节后重试", 502)

    return {
        "title": title,
        "objective": objective,
        "deliverable": deliverable,
        "redlines": _as_str_list(raw.get("redlines")),
        "acceptance": _as_str_list(raw.get("acceptance")),
        "terminology": terminology,
        "subtasks": subtasks,
    }


def _plan_messages(roster: str, instruction: str) -> list[dict]:
    return [
        {"role": "system",
         "content": "你是企业 CEO，负责把老板的一句话变成可执行的准绳与分工方案。"},
        {"role": "user",
         "content": PLAN_PROMPT.format(instruction=instruction, roster=roster,
                                       max_subtasks=MAX_SUBTASKS)},
    ]


async def create_mission(db: AsyncSession, *, company_id: str, user_id: str,
                         instruction: str, title: str | None = None,
                         coordinator_agent_id: str | None = None,
                         max_parallel: int | None = None,
                         parent_task_id: str | None = None) -> dict:
    """一句话 → 准绳 + 拆解预览（不建任务、不执行）。

    ``parent_task_id`` 供旧 /orchestrate 入口复用已经建好的父任务，
    不为兼容再凭空多造一条。
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise CeoError("请先说要办的事", 400)
    if len(instruction) > 4000:
        raise CeoError("指令太长了，请精简到 4000 字以内", 400)

    coord = await pick_coordinator(db, company_id, coordinator_agent_id)
    capacity = await resolve_capacity(db, str(coord.id), max_parallel)
    roster, agents = await _roster_lines(db, company_id)

    try:
        raw = await _chat(db, coord, _plan_messages(roster, instruction))
        plan = _normalize_plan(_extract_json(raw), instruction, agents)
    except CeoError:
        raise
    except Exception as exc:  # noqa: BLE001 — 模型/JSON 失败统一给可操作提示
        logger.warning("CEO 拆解失败 company=%s: %s", company_id, exc, exc_info=True)
        raise CeoError(f"CEO 拆解失败：{str(exc)[:200]}。请检查模型配置后重试", 502)

    if title:
        plan["title"] = title.strip()[:120]

    parent = None
    if parent_task_id:
        parent = await db.get(Task, parent_task_id)
        if parent is None or parent.company_id != company_id:
            raise CeoError("父任务不存在", 404)
    if parent is None:
        parent = Task(company_id=company_id, title=f"CEO：{plan['title']}",
                      description=instruction, status=TaskStatus.PLANNING,
                      priority=TaskPriority.NORMAL, assigned_agent_id=coord.id,
                      created_by=user_id)
        db.add(parent)
        await db.flush()

    mission = CeoMission(
        company_id=company_id, parent_task_id=parent.id,
        coordinator_agent_id=coord.id, created_by=user_id,
        title=plan["title"], instruction=instruction, status="PLANNING",
        objective=plan["objective"], deliverable=plan["deliverable"],
        redlines=plan["redlines"], acceptance=plan["acceptance"],
        terminology=plan["terminology"], extra_instructions=[],
        anchor_version=1,
        max_parallel=capacity["requested"],
        effective_parallel=capacity["effective"],
        provider_cap=capacity["provider_cap"],
        plan=plan["subtasks"], subtask_count=len(plan["subtasks"]),
        degraded=False,
    )
    db.add(mission)
    await db.flush()
    await log_event(db, "ceo.mission_planned", "ceo_mission", mission.id,
                    "user", user_id,
                    {"title": plan["title"], "subtasks": len(plan["subtasks"]),
                     "effective_parallel": capacity["effective"]},
                    company_id=company_id)
    await db.commit()
    await db.refresh(mission)
    return mission_preview(mission, capacity, coord)


def mission_preview(mission: CeoMission, capacity: dict | None,
                    coord: Agent | None = None) -> dict:
    capacity = capacity or {}
    return {
        "mission_id": mission.id,
        "title": mission.title,
        "status": mission.status,
        "objective": mission.objective or "",
        "deliverable": mission.deliverable or "",
        "redlines": mission.redlines or [],
        "acceptance": mission.acceptance or [],
        "terminology": mission.terminology or {},
        "extra_instructions": mission.extra_instructions or [],
        "anchor_version": mission.anchor_version,
        "subtasks": mission.plan or [],
        "subtask_count": mission.subtask_count,
        "coordinator_agent_id": mission.coordinator_agent_id or "",
        "coordinator_agent_name": coord.name if coord else "",
        "max_parallel": mission.max_parallel,
        "effective_parallel": mission.effective_parallel or mission.max_parallel,
        "provider_cap": mission.provider_cap or MIN_PARALLEL,
        "provider_type": capacity.get("provider_type", "unknown"),
        "min_parallel": MIN_PARALLEL,
        "hard_cap": HARD_CAP,
    }


# ---------------------------------------------------------------------------
# 开工
# ---------------------------------------------------------------------------

async def launch_mission(db: AsyncSession, mission_id: str,
                         requested_parallel: int | None = None,
                         submit: bool = True) -> dict:
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    if mission.status != "PLANNING":
        raise CeoError(f"当前状态 {mission.status} 不能开工", 409)

    capacity = await resolve_capacity(db, mission.coordinator_agent_id,
                                      requested_parallel or mission.max_parallel)
    mission.max_parallel = capacity["requested"]
    mission.effective_parallel = capacity["effective"]
    mission.provider_cap = capacity["provider_cap"]
    mission.status = "RUNNING"
    mission.started_at = mission.started_at or _iso()

    plan = mission.plan or []
    if not plan:
        raise CeoError("没有可执行的子任务，请重新拆解", 409)

    for item in plan[:MAX_SUBTASKS]:
        db.add(Task(
            company_id=mission.company_id,
            parent_task_id=mission.parent_task_id,
            mission_id=mission.id,
            title=item["title"][:500],
            description=item.get("description") or item["title"],
            status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
            assigned_agent_id=item.get("agent_id"),
            created_by=mission.created_by,
        ))
    parent = await db.get(Task, mission.parent_task_id) if mission.parent_task_id else None
    if parent and parent.status == TaskStatus.PLANNING:
        parent.status = TaskStatus.RUNNING
        parent.started_at = _iso()

    mission.subtask_count = len(plan[:MAX_SUBTASKS])
    await log_event(db, "ceo.mission_launched", "ceo_mission", mission.id,
                    "user", mission.created_by,
                    {"subtasks": mission.subtask_count,
                     "effective_parallel": mission.effective_parallel},
                    company_id=mission.company_id)
    await db.commit()

    import app.background_jobs as bg
    submitted = None if not submit else bg.submit_async(
        lambda session: run_mission(session, mission_id),
        timeout_s=MISSION_TIMEOUT_S, kind="ceo_mission")
    if submitted is bg.QUEUE_FULL:
        mission.status = "PLANNING"
        await db.commit()
        raise CeoError("后台任务队列已满，请稍后再试", 503)

    return {
        "mission_id": mission.id,
        "status": mission.status,
        "subtask_count": mission.subtask_count,
        "max_parallel": mission.max_parallel,
        "effective_parallel": mission.effective_parallel,
        "provider_cap": mission.provider_cap,
        "provider_type": capacity["provider_type"],
    }


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def deterministic_off_track_reason(output: str) -> str | None:
    """零成本自检：空/极短产出不用再花一次模型调用去发现。"""
    text = (output or "").strip()
    if not text:
        return "没有产出任何内容"
    if len(text) < MIN_RESULT_CHARS:
        return f"产出过短（{len(text)} 字），无法作为交付"
    return None


def _agent_result_rows(results: list[dict]) -> tuple[list[str], list[str]]:
    ok, problems = [], []
    for r in results:
        if r.get("status") == "COMPLETED" and r.get("output"):
            ok.append(f"## {r.get('title')}\n（执行者: {r.get('agent_name')}）\n\n"
                      f"{r.get('output')}\n")
        else:
            reason = r.get("error") or r.get("off_track_reason") or r.get("status")
            problems.append(f"- {r.get('title')}（{r.get('agent_name')}）：{reason}")
    return ok, problems


async def execute_subtask(task_id: str, agent_id: str,
                          anchor_block: str) -> dict:
    """单条子任务：独立 session 执行，避免并行时共享 ORM 缓存。

    测试直接 monkeypatch 这个函数，不需要连模型。
    """
    from app.agent_runtime import run_agent_on_task
    from app.background_jobs import _make_session_factory, dispose_factory_engine

    factory = _make_session_factory()
    try:
        async with factory() as s:
            task = await s.get(Task, task_id)
            agent = await s.get(Agent, agent_id)
            if task is None or agent is None:
                return {"task_id": task_id, "agent_id": agent_id, "title": "",
                        "agent_name": "", "status": "FAILED",
                        "error": "子任务或执行员工已不存在"}
            if task.status not in (TaskStatus.TODO, TaskStatus.PLANNING):
                return {"task_id": task_id, "agent_id": agent_id,
                        "title": task.title, "agent_name": agent.name,
                        "status": "SKIPPED", "error": f"状态 {task.status.value} 不再执行"}
            await run_agent_on_task(s, agent, task, actor_type="agent",
                                    extra_system=[anchor_block])
            task = await s.get(Task, task_id)
            output = (task.result or "").strip() if task else ""
            reason = deterministic_off_track_reason(output)
            if reason:
                task.current_stage = f"off_track: {reason}"[:200]
                await transition(s, task, TaskStatus.COMPLETED, "system", None,
                                 payload={"off_track": reason})
            else:
                await transition(s, task, TaskStatus.COMPLETED, "agent", agent_id)
            await s.commit()
            return {"task_id": task_id, "agent_id": agent_id, "title": task.title,
                    "agent_name": agent.name,
                    "status": "OFF_TRACK" if reason else "COMPLETED",
                    "output": output, "off_track_reason": reason}
    except Exception as exc:  # noqa: BLE001 — 子任务失败不能拖垮整场
        logger.error("CEO 子任务失败 task=%s agent=%s: %s", task_id, agent_id,
                     exc, exc_info=True)
        try:
            async with factory() as s2:
                t2 = await s2.get(Task, task_id)
                a2 = await s2.get(Agent, agent_id)
                if t2 and t2.status == TaskStatus.RUNNING:
                    t2.status = TaskStatus.FAILED
                if a2 and a2.status == AgentStatus.RUNNING:
                    a2.status = AgentStatus.IDLE
                await log_event(s2, "ceo.subtask_failed", "task", task_id,
                                "agent", agent_id, {"error": str(exc)[:500]})
                await s2.commit()
        except Exception as mark_err:  # noqa: BLE001
            logger.error("CEO 子任务失败标记也失败 task=%s: %s", task_id, mark_err)
        return {"task_id": task_id, "agent_id": agent_id, "title": "",
                "agent_name": "", "status": "FAILED", "error": str(exc)[:500],
                "failure_kind": failure_kind(exc)}
    finally:
        await dispose_factory_engine(factory)


async def _mission_status(mission_id: str) -> str:
    from app.background_jobs import _make_session_factory, dispose_factory_engine
    factory = _make_session_factory()
    try:
        async with factory() as s:
            row = await s.get(CeoMission, mission_id)
            return row.status if row else "MISSING"
    finally:
        await dispose_factory_engine(factory)


async def _run_one(task: Task, gate: DynamicGate, anchor: str,
                   mission_id: str) -> dict:
    async with gate.slot():
        # 真正开跑前再读一次状态：暂停/取消要让已排队的子任务停下
        if await _mission_status(mission_id) != "RUNNING":
            return {"task_id": task.id, "agent_id": task.assigned_agent_id or "",
                    "title": task.title, "agent_name": "",
                    "status": "SKIPPED", "error": "任务已暂停或取消"}
        try:
            out = await execute_subtask(task.id, str(task.assigned_agent_id), anchor)
        except Exception as exc:  # noqa: BLE001 — execute_subtask 已兜底，这里防未预期
            out = {"task_id": task.id, "agent_id": task.assigned_agent_id or "",
                   "title": task.title, "agent_name": "",
                   "status": "FAILED", "error": str(exc)[:500],
                   "failure_kind": failure_kind(exc)}
        if out.get("status") == "FAILED":
            degrade = await gate.note_failure(out.get("failure_kind") or "other")
            if degrade:
                await _mark_degraded(mission_id, degrade)
        else:
            await gate.note_success()
        return out


async def _mark_degraded(mission_id: str, info: dict) -> None:
    from app.background_jobs import _make_session_factory, dispose_factory_engine
    factory = _make_session_factory()
    try:
        async with factory() as s:
            m = await s.get(CeoMission, mission_id)
            if not m:
                return
            m.degraded = True
            m.degrade_reason = info.get("reason")
            m.degraded_until = info.get("until")
            m.effective_parallel = info.get("limit") or MIN_PARALLEL
            await log_event(s, "ceo.degraded", "ceo_mission", mission_id,
                            "system", None, info, company_id=m.company_id)
            await s.commit()
    finally:
        await dispose_factory_engine(factory)


async def _collect_finished(db: AsyncSession, mission_id: str) -> list[dict]:
    """收集本场统筹里**已经结束**的子任务产出。

    暂停/恢复会开一次新的 `run_mission`：上一轮已经交付的子任务不会再出现在
    `TODO` 查询里。若不把它们捡回来，恢复后的报告会只剩后半段，老板看到的是
    「一份缺了前半截的汇总」。同标题的「补跑：」结果优先，避免原偏离结果与
    补跑结果被算成两份。
    """
    rows = (await db.execute(
        select(Task).where(Task.mission_id == mission_id,
                           Task.status.in_([TaskStatus.COMPLETED,
                                            TaskStatus.FAILED]))
        .order_by(Task.created_at)
        .execution_options(populate_existing=True))).scalars().all()
    if not rows:
        return []
    agent_ids = {t.assigned_agent_id for t in rows if t.assigned_agent_id}
    names: dict[str, str] = {}
    if agent_ids:
        names = {str(i): n for i, n in (await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()}

    by_title: dict[str, dict] = {}
    for t in rows:
        stage = t.current_stage or ""
        if t.status == TaskStatus.FAILED:
            status, reason = "FAILED", stage or "执行失败"
        elif stage.startswith("off_track"):
            status = "OFF_TRACK"
            reason = stage.removeprefix("off_track:").strip() or "偏离准绳"
        else:
            status, reason = "COMPLETED", None
        base = t.title.removeprefix("补跑：")
        item = {
            "task_id": t.id, "agent_id": str(t.assigned_agent_id or ""),
            "title": base, "agent_name": names.get(str(t.assigned_agent_id), ""),
            "status": status, "output": t.result or "",
            "off_track_reason": reason,
        }
        if base not in by_title or t.title.startswith("补跑："):
            by_title[base] = item
    return list(by_title.values())


async def _review_outputs(db: AsyncSession, mission: CeoMission,
                          results: list[dict]) -> dict[str, dict]:
    """批量锚审查：一次调用给全部产出定「是否偏离」。失败则跳过。"""
    usable = [r for r in results if r.get("output")]
    if not usable:
        return {}
    payload = "\n\n".join(
        f"### {r.get('title')}（{r.get('agent_name')}）\n{r.get('output')[:2500]}"
        for r in usable[:MAX_SUBTASKS])
    agent = await db.get(Agent, mission.coordinator_agent_id)
    if agent is None:
        return {}
    try:
        raw = await _chat(db, agent, [
            {"role": "system", "content": "你是严格的交付审查人，只输出 JSON。"},
            {"role": "user", "content": REVIEW_PROMPT.format(
                anchor=build_anchor_block(mission), outputs=payload)},
        ], max_tokens=1200)
        data = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — 审查失败不阻断汇总，但必须留痕
        logger.warning("CEO 锚审查失败 mission=%s: %s", mission.id, exc)
        return {}
    out: dict[str, dict] = {}
    for item in data.get("reviews") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("title") or "").strip()
        if key:
            out[key] = {
                "on_track": bool(item.get("on_track", True)),
                "reason": str(item.get("reason") or "")[:500],
            }
    return out


async def _retry_off_track(db: AsyncSession, mission: CeoMission,
                           tasks: list[Task]) -> list[Task]:
    """把失败/偏离的子任务补跑一次；返回新建的补跑任务。"""
    if mission.retry_count >= MAX_RETRY:
        return []
    created: list[Task] = []
    for t in tasks:
        reason = t.current_stage or ""
        if t.status == TaskStatus.COMPLETED and not reason.startswith("off_track"):
            continue
        if t.status not in (TaskStatus.FAILED, TaskStatus.COMPLETED):
            continue
        desc = ((t.description or "") + "\n\n"
                f"[补跑 of {t.id}] 上一轮问题：{reason or '执行失败'}。"
                "这次必须先修正该问题，再交付。")
        retry = Task(company_id=mission.company_id,
                     parent_task_id=mission.parent_task_id,
                     mission_id=mission.id,
                     title=f"补跑：{t.title}"[:500], description=desc,
                     status=TaskStatus.TODO, priority=TaskPriority.HIGH,
                     assigned_agent_id=t.assigned_agent_id,
                     created_by=mission.created_by)
        db.add(retry)
        created.append(retry)
    if created:
        mission.retry_count += 1
        await db.flush()
        await log_event(db, "ceo.retry_scheduled", "ceo_mission", mission.id,
                        "system", None, {"count": len(created)},
                        company_id=mission.company_id)
    return created


async def _finalize(db: AsyncSession, mission: CeoMission,
                    results: list[dict]) -> None:
    ok, problems = _agent_result_rows(results)
    mission.completed_count = sum(1 for r in results if r.get("status") == "COMPLETED")
    mission.off_track_count = sum(1 for r in results if r.get("status") == "OFF_TRACK")
    mission.failed_count = sum(
        1 for r in results if r.get("status") in ("FAILED", "SKIPPED"))

    report = ""
    agent = await db.get(Agent, mission.coordinator_agent_id)
    if agent is not None:
        try:
            report = await _chat(db, agent, [
                {"role": "system", "content": "你是企业 CEO，向老板做最终汇报。"},
                {"role": "user", "content": SUMMARY_PROMPT.format(
                    anchor=build_anchor_block(mission),
                    results="\n".join(ok) or "（无）",
                    problems="\n".join(problems) or "（无）")},
            ], max_tokens=2400)
        except Exception as exc:  # noqa: BLE001 — 汇总失败也要给老板一份可读结果
            logger.warning("CEO 汇总失败 mission=%s: %s", mission.id, exc)
    if not report:
        report = ("## 结论\n汇总生成失败，以下为各子任务的原始产出。\n\n"
                  + ("\n".join(ok) or "（无产出）")
                  + ("\n\n## 风险与建议\n" + "\n".join(problems) if problems else ""))

    if problems:
        report += "\n\n---\n## 需要你关注\n" + "\n".join(problems)

    mission.report = report
    mission.finished_at = _iso()
    succeeded = [r for r in results if r.get("status") in ("COMPLETED", "OFF_TRACK")]
    mission.status = "COMPLETED" if succeeded else "FAILED"
    mission.stats = {
        "subtasks": len(results),
        "completed": mission.completed_count,
        "off_track": mission.off_track_count,
        "failed": mission.failed_count,
        "retried": mission.retry_count,
        "effective_parallel": mission.effective_parallel,
        "degraded": bool(mission.degraded),
    }

    parent = await db.get(Task, mission.parent_task_id) if mission.parent_task_id else None
    if parent is not None:
        parent.result = report
        target = TaskStatus.COMPLETED if mission.status == "COMPLETED" else TaskStatus.FAILED
        try:
            await transition(db, parent, target, "agent", mission.coordinator_agent_id,
                             commit=False)
        except Exception as exc:  # noqa: BLE001 — 父任务状态已变，不覆盖
            logger.warning("CEO 父任务收尾失败 task=%s: %s", parent.id, exc)

    try:
        from app.api.routes.assets import save_artifact
        asset = await save_artifact(
            db, mission.company_id,
            f"CEO汇总报告-{mission.title[:40]}.md", report,
            task_id=mission.parent_task_id, agent_id=mission.coordinator_agent_id)
        await db.flush()
        mission.report_artifact_id = asset.id
    except Exception as exc:  # noqa: BLE001 — 归档失败不影响报告本身
        logger.warning("CEO 报告归档失败 mission=%s: %s", mission.id, exc)

    try:
        from app.services.notification_service import notify
        await notify(db, user_id=mission.created_by, company_id=mission.company_id,
                     type="ceo.mission_done",
                     title=f"CEO 已交付：{mission.title[:40]}",
                     body=(f"完成 {mission.completed_count}/{len(results)}，"
                           f"偏离 {mission.off_track_count}，失败 {mission.failed_count}"),
                     link=f"/ceo?mission={mission.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CEO 完成通知失败 mission=%s: %s", mission.id, exc)


async def run_mission(db: AsyncSession, mission_id: str) -> dict:
    """后台执行一场统筹。已暂停/已终结时直接返回，不重复跑。"""
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        return {"mission_id": mission_id, "status": "MISSING"}
    if mission.status in STATUS_TERMINAL:
        return {"mission_id": mission_id, "status": mission.status, "skipped": True}

    mission.status = "RUNNING"
    mission.started_at = mission.started_at or _iso()
    anchor = build_anchor_block(mission)
    effective = mission.effective_parallel or mission.max_parallel or DEFAULT_PARALLEL
    gate = DynamicGate(effective, base_limit=mission.max_parallel)
    # 长耗时前先释放写事务，否则 SQLite 下子任务会话会被本事务堵住
    await db.commit()

    tasks = (await db.execute(
        select(Task).where(Task.mission_id == mission_id,
                           Task.status == TaskStatus.TODO)
        .order_by(Task.created_at))).scalars().all()

    # 恢复运行时，上一轮已交付的子任务不在 TODO 里；先捡回来，
    # 否则最终报告会缺前半段（见 `_collect_finished` 的说明）。
    results: list[dict] = await _collect_finished(db, mission_id)
    if tasks:
        results += list(await asyncio.gather(
            *[_run_one(t, gate, anchor, mission_id) for t in tasks]))
    await db.refresh(mission)

    if mission.status in ("PAUSED", "CANCELLED"):
        await db.commit()
        return {"mission_id": mission_id, "status": mission.status,
                "executed": len(results)}

    mission.status = "RECLAIMING"
    await db.commit()

    # 批量锚审查（一次调用）——只对真正有产出的结果
    reviews = await _review_outputs(db, mission, results)
    for r in results:
        verdict = reviews.get(str(r.get("title") or ""))
        if verdict and not verdict["on_track"]:
            r["status"] = "OFF_TRACK"
            r["off_track_reason"] = verdict["reason"] or "偏离准绳"

    # 补跑：失败/偏离各一次。
    # populate_existing 是必需的：子任务在各自 session 里执行并提交，本
    # session 身份映射里还留着它们开工前的 TODO 快照；不强制刷新的话，
    # 这里会把「已完成/已偏离」读成「还没跑」，补跑永远不触发。
    mission_tasks = (await db.execute(
        select(Task).where(Task.mission_id == mission_id)
        .execution_options(populate_existing=True))).scalars().all()
    retry_tasks = await _retry_off_track(db, mission, mission_tasks)
    await db.commit()

    if retry_tasks:
        # 补跑也要经过闸门的 RUNNING 校验：RECLAIMING 是"正在回收"，不是
        # "已停下"，但 _run_one 只放行 RUNNING——不先切回来，补跑会被
        # 自己判成「任务已暂停或取消」而全部 SKIPPED（补跑形同虚设）。
        mission.status = "RUNNING"
        await db.commit()
        retry_results = list(await asyncio.gather(
            *[_run_one(t, gate, anchor, mission_id) for t in retry_tasks]))
        # 补跑结果按标题（去掉「补跑：」前缀）替换原结果：同一条子任务只留
        # 最新一次尝试，否则原偏离与补跑会被算成两条，统计翻倍。
        by_title = {str(r.get("title") or ""): r for r in results}
        for rr in retry_results:
            key = str(rr.get("title") or "").removeprefix("补跑：")
            rr["title"] = key
            by_title[key] = rr
        results = list(by_title.values())

    await db.refresh(mission)
    if mission.status in ("PAUSED", "CANCELLED"):
        await db.commit()
        return {"mission_id": mission_id, "status": mission.status}

    await _finalize(db, mission, results)
    await db.commit()
    await db.refresh(mission)
    return {"mission_id": mission.id, "status": mission.status,
            "stats": mission.stats}


# ---------------------------------------------------------------------------
# 查询 / 操作
# ---------------------------------------------------------------------------

def mission_list_item(mission: CeoMission, coordinator_name: str = "") -> dict:
    return {
        "mission_id": mission.id,
        "title": mission.title,
        "status": mission.status,
        "subtask_count": mission.subtask_count,
        "completed_count": mission.completed_count,
        "failed_count": mission.failed_count,
        "off_track_count": mission.off_track_count,
        "coordinator_agent_id": mission.coordinator_agent_id or "",
        "coordinator_agent_name": coordinator_name,
        "effective_parallel": mission.effective_parallel or mission.max_parallel,
        "degraded": bool(mission.degraded),
        "created_at": mission.created_at.isoformat() if mission.created_at else "",
        "finished_at": mission.finished_at or "",
    }


def mission_detail(mission: CeoMission, tasks: list[Task],
                   agents: dict[str, Agent]) -> dict:
    slots = []
    for t in tasks:
        agent = agents.get(str(t.assigned_agent_id))
        stage = t.current_stage or ""
        slots.append({
            "task_id": t.id,
            "title": t.title,
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "agent_id": str(t.assigned_agent_id or ""),
            "agent_name": agent.name if agent else "",
            "progress": float(t.progress or 0.0),
            "stage": stage,
            "off_track": stage.startswith("off_track"),
            "result_excerpt": (t.result or "")[:600],
            "started_at": t.started_at,
            "completed_at": t.completed_at,
        })
    return {
        "mission_id": mission.id,
        "title": mission.title,
        "instruction": mission.instruction,
        "status": mission.status,
        "objective": mission.objective or "",
        "deliverable": mission.deliverable or "",
        "redlines": mission.redlines or [],
        "acceptance": mission.acceptance or [],
        "terminology": mission.terminology or {},
        "extra_instructions": mission.extra_instructions or [],
        "anchor_version": mission.anchor_version,
        "coordinator_agent_id": mission.coordinator_agent_id or "",
        "coordinator_agent_name": (
            agents[str(mission.coordinator_agent_id)].name
            if mission.coordinator_agent_id
            and str(mission.coordinator_agent_id) in agents else ""),
        "subtask_count": mission.subtask_count,
        "completed_count": mission.completed_count,
        "failed_count": mission.failed_count,
        "off_track_count": mission.off_track_count,
        "retry_count": mission.retry_count,
        "max_parallel": mission.max_parallel,
        "effective_parallel": mission.effective_parallel or mission.max_parallel,
        "provider_cap": mission.provider_cap or MIN_PARALLEL,
        "degraded": bool(mission.degraded),
        "degrade_reason": mission.degrade_reason or "",
        "degraded_until": mission.degraded_until or "",
        "stats": mission.stats or {},
        "report_excerpt": (mission.report or "")[:400],
        "started_at": mission.started_at or "",
        "finished_at": mission.finished_at or "",
        "slots": slots,
    }


async def pause_mission(db: AsyncSession, mission_id: str) -> dict:
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    if mission.status not in ("RUNNING", "RECLAIMING"):
        raise CeoError(f"当前状态 {mission.status} 不能暂停", 409)
    mission.status = "PAUSED"
    await log_event(db, "ceo.paused", "ceo_mission", mission.id, "user",
                    mission.created_by, {}, company_id=mission.company_id)
    await db.commit()
    return {"mission_id": mission.id, "status": mission.status}


async def resume_mission(db: AsyncSession, mission_id: str) -> dict:
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    if mission.status != "PAUSED":
        raise CeoError(f"当前状态 {mission.status} 不能继续", 409)
    mission.status = "RUNNING"
    await db.commit()
    import app.background_jobs as bg
    if bg.submit_async(lambda session: run_mission(session, mission_id),
                       timeout_s=MISSION_TIMEOUT_S, kind="ceo_mission") is bg.QUEUE_FULL:
        mission.status = "PAUSED"
        await db.commit()
        raise CeoError("后台任务队列已满，请稍后再试", 503)
    return {"mission_id": mission.id, "status": mission.status}


async def cancel_mission(db: AsyncSession, mission_id: str) -> dict:
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    if mission.status in STATUS_TERMINAL:
        raise CeoError(f"任务已结束（{mission.status}）", 409)
    mission.status = "CANCELLED"
    mission.finished_at = _iso()
    cancelled = await db.execute(
        update(Task)
        .where(Task.mission_id == mission_id,
               Task.status.in_([TaskStatus.TODO, TaskStatus.PLANNING]))
        .values(status=TaskStatus.CANCELLED)
        .execution_options(synchronize_session=False))
    parent = await db.get(Task, mission.parent_task_id) if mission.parent_task_id else None
    if parent and parent.status in (TaskStatus.PLANNING, TaskStatus.RUNNING,
                                    TaskStatus.PAUSED):
        parent.status = TaskStatus.CANCELLED
        parent.completed_at = _iso()
    await log_event(db, "ceo.cancelled", "ceo_mission", mission.id, "user",
                    mission.created_by, {"cancelled_tasks": cancelled.rowcount},
                    company_id=mission.company_id)
    await db.commit()
    return {"mission_id": mission.id, "status": mission.status,
            "cancelled_tasks": int(cancelled.rowcount or 0)}


async def append_instruction(db: AsyncSession, mission_id: str,
                             text: str) -> dict:
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    text = (text or "").strip()
    if not text:
        raise CeoError("指令不能为空", 400)
    if mission.status in STATUS_TERMINAL:
        raise CeoError("任务已结束，不能再追加指令", 409)
    mission.extra_instructions = list(mission.extra_instructions or []) + [text[:1000]]
    mission.anchor_version += 1
    await log_event(db, "ceo.instruction_appended", "ceo_mission", mission.id,
                    "user", mission.created_by, {"text": text[:200]},
                    company_id=mission.company_id)
    await db.commit()
    return {"mission_id": mission.id, "anchor_version": mission.anchor_version,
            "extra_instructions": mission.extra_instructions}


async def reclaim_mission(db: AsyncSession, mission_id: str) -> dict:
    """手动回收：把没交付/偏离的子任务再补跑一次，然后重新汇总。"""
    mission = await db.get(CeoMission, mission_id)
    if mission is None:
        raise CeoError("任务不存在", 404)
    if mission.status not in ("COMPLETED", "FAILED"):
        raise CeoError(f"当前状态 {mission.status} 不需要补跑", 409)
    mission.retry_count = 0  # 手动补跑不受自动补跑次数限制
    tasks = (await db.execute(
        select(Task).where(Task.mission_id == mission_id))).scalars().all()
    retries = await _retry_off_track(db, mission, tasks)
    if not retries:
        raise CeoError("没有需要补跑的子任务", 409)
    mission.status = "RUNNING"
    mission.finished_at = None
    await db.commit()
    import app.background_jobs as bg
    if bg.submit_async(lambda session: run_mission(session, mission_id),
                       timeout_s=MISSION_TIMEOUT_S, kind="ceo_mission") is bg.QUEUE_FULL:
        mission.status = "FAILED"
        await db.commit()
        raise CeoError("后台任务队列已满，请稍后再试", 503)
    return {"mission_id": mission.id, "status": mission.status,
            "retry_tasks": len(retries)}


async def set_ceo_agent(db: AsyncSession, company_id: str,
                        agent_id: str) -> dict:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.company_id != company_id:
        raise CeoError("员工不存在或不属于这家公司", 404)
    await db.execute(update(Agent).where(Agent.company_id == company_id)
                     .values(is_ceo=False)
                     .execution_options(synchronize_session=False))
    agent.is_ceo = True
    await log_event(db, "ceo.agent_assigned", "agent", agent.id, "user", None,
                    {"agent_name": agent.name}, company_id=company_id)
    await db.commit()
    return {"company_id": company_id, "ceo_agent_id": agent.id,
            "ceo_agent_name": agent.name}


# ---------------------------------------------------------------------------
# 定时数据汇总（CEO 日报）
# ---------------------------------------------------------------------------

async def build_digest(db: AsyncSession, company_id: str,
                       window_hours: int = DIGEST_WINDOW_HOURS) -> dict:
    since = _utcnow().replace(tzinfo=None) - timedelta(hours=window_hours)

    mission_rows = (await db.execute(
        select(CeoMission.status, func.count())
        .where(CeoMission.company_id == company_id,
               CeoMission.created_at >= since)
        .group_by(CeoMission.status))).all()
    mission_counts = {status: count for status, count in mission_rows}

    task_rows = (await db.execute(
        select(Task.status, func.count())
        .where(Task.company_id == company_id, Task.created_at >= since)
        .group_by(Task.status))).all()
    task_counts = {(s.value if hasattr(s, "value") else str(s)): c
                   for s, c in task_rows}

    fail_rows = (await db.execute(
        select(Agent.name, func.count())
        .join(Task, Task.assigned_agent_id == Agent.id)
        .where(Task.company_id == company_id,
               Task.status == TaskStatus.FAILED,
               Task.created_at >= since)
        .group_by(Agent.name)
        .order_by(func.count().desc())
        .limit(3))).all()
    top_failing = [{"agent": name or "未定岗", "failed": count}
                   for name, count in fail_rows]

    usage = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0),
               func.coalesce(func.sum(TokenUsage.total_cost), 0.0))
        .where(TokenUsage.company_id == company_id,
               TokenUsage.created_at >= since))).first()
    tokens, cost = int(usage[0] or 0), float(usage[1] or 0.0)

    stuck_threshold = (_utcnow() - timedelta(minutes=30)).isoformat()
    stuck = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.company_id == company_id,
            Task.status == TaskStatus.RUNNING,
            Task.started_at.is_not(None),
            Task.started_at < stuck_threshold))).scalar() or 0

    total_missions = sum(mission_counts.values())
    done = mission_counts.get("COMPLETED", 0)
    lines = [
        f"# CEO 日报 · 近 {window_hours} 小时",
        "",
        "## 统筹",
        f"- 新开任务 {total_missions} 场，完成 {done} 场，"
        f"失败 {mission_counts.get('FAILED', 0)} 场，"
        f"进行中 {mission_counts.get('RUNNING', 0) + mission_counts.get('PLANNING', 0) + mission_counts.get('RECLAIMING', 0) + mission_counts.get('PAUSED', 0)} 场",
        "",
        "## 执行",
        f"- 新建任务 {sum(task_counts.values())} 条，"
        f"完成 {task_counts.get('COMPLETED', 0)} 条，"
        f"失败 {task_counts.get('FAILED', 0)} 条，"
        f"进行中 {task_counts.get('RUNNING', 0)} 条",
        f"- 卡住超过 30 分钟：{stuck} 条",
        "",
        "## 成本",
        f"- 消耗 {tokens:,} tokens，约 ¥{cost:.2f}",
        "",
        "## 需要你关注",
    ]
    if stuck:
        lines.append(f"- {stuck} 条任务长时间停在运行中，建议打开「任务」排查或接管")
    if top_failing:
        lines.append("- 失败最多的员工：" + "、".join(
            f"{x['agent']}（{x['failed']} 次）" for x in top_failing))
    if not stuck and not top_failing:
        lines.append("- 无异常")
    markdown = "\n".join(lines)

    return {
        "company_id": company_id,
        "generated_at": _iso(),
        "window_hours": window_hours,
        "markdown": markdown,
        "stats": {
            "missions": mission_counts,
            "tasks": task_counts,
            "tokens": tokens,
            "cost": round(cost, 4),
            "stuck_tasks": int(stuck),
            "top_failing_agents": top_failing,
        },
    }


async def push_digest(db: AsyncSession, company_id: str) -> dict:
    digest = await build_digest(db, company_id)
    from app.services.notification_service import notify_company_admins
    await notify_company_admins(
        db, company_id=company_id, type="ceo.digest",
        title="CEO 日报已生成", body=digest["markdown"][:400], link="/ceo")
    await db.commit()
    return digest


async def ensure_digest_schedule(db: AsyncSession, company_id: str,
                                 user_id: str, time_str: str = "09:00",
                                 name: str = "CEO 运行日报") -> dict:
    """确保每天有一条 ceo_digest 调度；已存在则原样返回（幂等）。"""
    from app.models.scheduler import AutomationSchedule, ScheduleStatus, compute_next_run
    from datetime import datetime as _dt

    existing = (await db.execute(
        select(AutomationSchedule).where(
            AutomationSchedule.company_id == company_id,
            AutomationSchedule.action_type == "ceo_digest",
            AutomationSchedule.status != ScheduleStatus.ARCHIVED)
        .limit(1))).scalars().first()
    if existing:
        return {"schedule_id": existing.id, "trigger_config": existing.trigger_config or {},
                "created": False}

    now = _dt.now(timezone.utc).replace(tzinfo=None)
    cfg = {"time": time_str}
    row = AutomationSchedule(
        id=uuid.uuid4().hex,
        company_id=company_id, name=name,
        trigger_type="daily", trigger_config=cfg,
        action_type="ceo_digest", action_params={"time": time_str},
        status=ScheduleStatus.ACTIVE,
        next_run_at=compute_next_run("daily", cfg, now),
        created_by=user_id)
    db.add(row)
    await db.commit()
    return {"schedule_id": row.id, "trigger_config": cfg, "created": True}
