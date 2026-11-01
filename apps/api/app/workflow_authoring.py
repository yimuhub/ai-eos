"""CEO 工作流生成与自优化。

## 为什么不是「让模型随便画一张图」

老板要的不是一张好看的流程图，而是**下次同类任务能直接跑得更好**的作业书。
所以这里做三件有明确输入输出的事：

1. **生成**（开工时）：准绳 + 拆解 + 知识库检索 + 公开网络调研 → 一张可执行图。
   模型输出会逐项校验（节点类型、连线、可达性、执行人存在性），不合法就退到
   确定性图（START → 每个子任务一个 AGENT → END），保证开工时一定有工作流。
2. **评分**（收尾时）：确定性公式，不用模型，老板可以自己复算；
   失败/偏离/补跑分别扣分，结果写进 `workflow_feedback`。
3. **升级**（评分低于阈值时）：复制上一版，把本次总结出的经验注入每个执行节点，
   版本 +1、旧版归档；同一 `reuse_key` 下次生成时会把历史经验带进提示词。

## 复用键

`reuse_key` 由任务名归一化而来（去空白与标点、小写）。同名任务视为同一类流程，
下一个版本会继承上一版的节点与经验；不同名的任务不会互相污染。

## 安全与成本

* 知识库检索走现有的 `cost_optimized_rag`（本地、有预算截断）；
* 网络调研默认开启，6 秒超时、只取 3 条摘要；失败只记 warning；
* 模型生成/升级都包了超时与异常，任何失败都退到确定性实现，不阻断开工/收尾；
* 测试环境用 `AIEOS_CEO_WORKFLOW_AUTHOR/LLM` 关闭外呼，分支用 monkeypatch 覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.ceo import CeoMission
from app.models.workflow import (NodeType, WorkflowDefinition, WorkflowEdge,
                                 WorkflowFeedback, WorkflowNode, WorkflowStatus)
from app.services.event_system import log_event

logger = get_logger(__name__)

MAX_NODES = 24
MAX_VERSION = 9
OPTIMIZE_THRESHOLD = 80.0
SUCCESS_THRESHOLD = 60.0
WEB_TIMEOUT_S = 6.0
AUTHOR_TIMEOUT_S = 25.0

_GRAPH_TYPES = {
    "START", "END", "AGENT", "APPROVAL", "CONDITION", "WAIT", "TASK",
    "PARALLEL", "MERGE",
}


def _env_on(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reuse_key_for(title: str) -> str:
    """任务名归一化成复用键：同名任务共享同一条工作流谱系。"""
    key = re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)
    return (key or "untitled")[:200]


def _plan_signature(plan: list | None) -> str:
    return "|".join(str(st.get("title") or "") for st in (plan or []))[:2000]


def workflow_brief(wf: WorkflowDefinition | None) -> dict | None:
    if wf is None:
        return None
    return {
        "id": wf.id,
        "name": wf.name,
        "version": wf.version,
        "source": wf.source,
        "status": wf.status.value if hasattr(wf.status, "value") else str(wf.status),
        "score": wf.score,
        "run_count": wf.run_count,
        "success_count": wf.success_count,
        "failure_count": wf.failure_count,
        "reuse_key": wf.reuse_key or "",
        "previous_workflow_id": wf.previous_workflow_id or "",
    }


# ---------------------------------------------------------------------------
# 上下文：知识库 + 网络调研
# ---------------------------------------------------------------------------

async def gather_context(db: AsyncSession, mission: CeoMission,
                         coordinator: Agent | None) -> dict:
    """收集生成工作流所需的公司知识与外部资料（全部 best-effort）。"""
    out: dict = {"kb_text": "", "kb_hits": 0, "web_text": "",
                 "web_sources": [], "errors": []}
    query = f"{mission.title} {mission.objective or ''}".strip()[:500]

    try:
        from app.kb_engine import cost_optimized_rag
        rag = await cost_optimized_rag(
            db, mission.company_id, mission.coordinator_agent_id, query,
            agent=coordinator)
        out["kb_text"] = str(rag.get("context_text") or "")[:4000]
        out["kb_hits"] = int(rag.get("hits") or 0)
    except Exception as exc:  # noqa: BLE001 — 没有知识库也能生成工作流
        logger.warning("工作流生成：知识库检索失败 mission=%s: %s",
                       mission.id, exc)
        out["errors"].append(f"知识库检索失败：{type(exc).__name__}")

    if not query or not _env_on("AIEOS_CEO_WEB_RESEARCH", True):
        return out

    try:
        from app.skill_system import WebSearchSkill

        async def _search() -> dict:
            return await WebSearchSkill().execute(
                {"query": query[:180], "max_results": 3})

        payload = (await asyncio.wait_for(_search(), timeout=WEB_TIMEOUT_S)
                   or {}).get("result") or {}
        lines: list[str] = []
        for item in (payload.get("results") or [])[:3]:
            title = str(item.get("title") or "").strip()[:120]
            snippet = str(item.get("snippet") or item.get("body") or "").strip()[:400]
            url = str(item.get("url") or item.get("href") or "").strip()[:300]
            if not (title or snippet):
                continue
            lines.append(f"- {title}：{snippet}（{url}）")
            out["web_sources"].append({"title": title, "url": url})
        out["web_text"] = "\n".join(lines)
    except asyncio.TimeoutError:
        logger.warning("工作流生成：网络调研超时 mission=%s", mission.id)
        out["errors"].append("网络调研超时")
    except Exception as exc:  # noqa: BLE001 — 外网失败不影响生成
        logger.warning("工作流生成：网络调研失败 mission=%s: %s",
                       mission.id, exc)
        out["errors"].append(f"网络调研失败：{type(exc).__name__}")
    return out


# ---------------------------------------------------------------------------
# 历史经验
# ---------------------------------------------------------------------------

async def latest_workflow(db: AsyncSession, company_id: str,
                          reuse_key: str) -> WorkflowDefinition | None:
    return (await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.company_id == company_id,
               WorkflowDefinition.reuse_key == reuse_key)
        .order_by(WorkflowDefinition.version.desc(),
                  WorkflowDefinition.created_at.desc())
        .limit(1))).scalars().first()


async def collect_lessons(db: AsyncSession,
                          workflow: WorkflowDefinition | None,
                          limit: int = 10) -> list[str]:
    """把历史反馈里的经验去重后取最近若干条。"""
    if workflow is None:
        return []
    lessons: list[str] = [str(x) for x in ((workflow.meta or {}).get("lessons") or [])]
    rows = (await db.execute(
        select(WorkflowFeedback)
        .where(WorkflowFeedback.workflow_id == workflow.id,
               WorkflowFeedback.success.is_(False))
        .order_by(WorkflowFeedback.created_at.desc())
        .limit(5))).scalars().all()
    for row in rows:
        lessons.extend(str(x) for x in (row.lessons or []))
    seen: set[str] = set()
    out: list[str] = []
    for text in lessons:
        text = str(text).strip()[:300]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out[-limit:]


# ---------------------------------------------------------------------------
# 图：确定性兜底 + 模型输出校验
# ---------------------------------------------------------------------------

def _anchor_summary(mission: CeoMission) -> str:
    lines = [f"目标：{mission.objective or mission.instruction}"]
    if mission.deliverable:
        lines.append(f"交付物：{mission.deliverable}")
    if mission.redlines:
        lines.append("红线：" + "；".join(str(x) for x in mission.redlines[:8]))
    if mission.acceptance:
        lines.append("验收：" + "；".join(str(x) for x in mission.acceptance[:8]))
    return "\n".join(lines)


def _node_instruction(mission: CeoMission, item: dict) -> str:
    parts = [str(item.get("description") or item.get("title") or "").strip()]
    skills = [str(s) for s in (item.get("required_skills") or []) if s]
    if skills:
        parts.append("必备技能：" + "、".join(skills[:5]))
    if mission.acceptance:
        parts.append("验收标准：" + "；".join(
            str(a) for a in mission.acceptance[:6]))
    if mission.redlines:
        parts.append("红线：" + "；".join(str(r) for r in mission.redlines[:6]))
    return "\n".join(p for p in parts if p)[:2000]


def deterministic_graph(mission: CeoMission, coordinator: Agent | None) -> dict:
    """START → 逐个子任务 AGENT → END。顺序执行，保证可跑可审计。"""
    nodes = [{"key": "start", "type": "START", "name": "开始",
              "x": 80, "y": 120}]
    edges: list[dict] = []
    prev = "start"
    plan = list(mission.plan or [])
    for i, item in enumerate(plan[:MAX_NODES - 2], 1):
        key = f"n{i}"
        agent_id = str(item.get("agent_id") or (coordinator.id if coordinator else ""))
        nodes.append({
            "key": key, "type": "AGENT",
            "name": str(item.get("title") or f"子任务{i}")[:120],
            "x": 80 + i * 230, "y": 120,
            "agent_id": agent_id,
            "task_title_template": str(item.get("title") or f"子任务{i}")[:200],
            "instruction": _node_instruction(mission, item),
        })
        edges.append({"from": prev, "to": key, "label": ""})
        prev = key
    nodes.append({"key": "end", "type": "END", "name": "结束",
                  "x": 80 + (len(nodes)) * 230, "y": 120})
    edges.append({"from": prev, "to": "end", "label": ""})
    return {"nodes": nodes, "edges": edges}


def _agent_lookup(mission: CeoMission, coordinator: Agent | None) -> dict[str, str]:
    """执行人姓名 → agent_id；未匹配的交给负责人。"""
    mapping: dict[str, str] = {}
    for item in mission.plan or []:
        name = str(item.get("agent_name") or "").strip().lower()
        agent_id = str(item.get("agent_id") or "").strip()
        if name and agent_id:
            mapping[name] = agent_id
    for item in mission.new_agents or []:
        for name in (item.get("name"), item.get("proposed_name")):
            name = str(name or "").strip().lower()
            agent_id = str(item.get("agent_id") or "").strip()
            if name and agent_id:
                mapping[name] = agent_id
    if coordinator is not None:
        coord_name = str(coordinator.name or "").strip().lower()
        if coord_name:
            mapping.setdefault(coord_name, str(coordinator.id))
    return mapping


def _resolve_agent(raw: str, lookup: dict[str, str],
                   coordinator: Agent | None) -> str:
    target = str(raw or "").strip().lower()
    if not target:
        return str(coordinator.id) if coordinator else ""
    if target in lookup:
        return lookup[target]
    for name, agent_id in lookup.items():
        if name and (name in target or target in name):
            return agent_id
    return str(coordinator.id) if coordinator else ""


def normalize_graph(raw: dict, mission: CeoMission,
                    coordinator: Agent | None) -> dict:
    """校验模型给的图：不合法直接抛错，由调用方退到确定性图。"""
    if not isinstance(raw, dict):
        raise ValueError("图不是对象")
    raw_nodes = raw.get("nodes")
    raw_edges = raw.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("缺少 nodes/edges")

    lookup = _agent_lookup(mission, coordinator)
    plan_by_title = {str(st.get("title") or "").strip().lower(): st
                     for st in (mission.plan or [])}
    nodes: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_nodes[:MAX_NODES]):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:50]
        node_type = str(item.get("type") or item.get("node_type") or "").upper()
        if not key or key in seen or node_type not in _GRAPH_TYPES:
            continue
        seen.add(key)
        name = str(item.get("name") or key)[:200]
        node = {"key": key, "type": node_type, "name": name,
                "x": float(item.get("x") or 80 + i * 230),
                "y": float(item.get("y") or 120)}
        if node_type == "AGENT":
            agent_id = _resolve_agent(
                item.get("agent") or item.get("agent_name") or "", lookup,
                coordinator)
            if not agent_id:
                continue
            instruction = str(item.get("instruction") or "").strip()
            matched = plan_by_title.get(name.strip().lower())
            if matched is not None:
                instruction = instruction or _node_instruction(mission, matched)
            node.update({
                "agent_id": agent_id,
                "task_title_template": str(
                    item.get("task_title_template") or name)[:200],
                "instruction": (instruction or name)[:2000],
            })
        elif node_type == "WAIT":
            try:
                node["seconds"] = max(0, min(int(item.get("seconds") or 5), 60))
            except (TypeError, ValueError):
                node["seconds"] = 5
        elif node_type == "APPROVAL":
            node["approver_hint"] = str(item.get("approver_hint") or "")[:200]
        elif node_type == "CONDITION":
            node["keyword"] = str(item.get("keyword") or "")[:100]
        nodes.append(node)

    types = {n["type"] for n in nodes}
    if "START" not in types or "END" not in types:
        raise ValueError("必须包含 START 与 END")

    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    for item in raw_edges[:MAX_NODES * 2]:
        if not isinstance(item, dict):
            continue
        src = str(item.get("from") or item.get("from_node") or "").strip()
        dst = str(item.get("to") or item.get("to_node") or "").strip()
        if src not in seen or dst not in seen or src == dst:
            continue
        if (src, dst) in seen_edges:
            continue
        seen_edges.add((src, dst))
        edges.append({"from": src, "to": dst,
                      "label": str(item.get("label") or "")[:100]})
    if not edges:
        raise ValueError("没有任何连线")

    # 可达性：START 必须能走到 END，否则这张图跑不到终点
    starts = [n["key"] for n in nodes if n["type"] == "START"]
    ends = {n["key"] for n in nodes if n["type"] == "END"}
    out: dict[str, list[str]] = {}
    for e in edges:
        out.setdefault(e["from"], []).append(e["to"])
    reachable: set[str] = set()
    stack = list(starts)
    while stack:
        key = stack.pop()
        if key in reachable:
            continue
        reachable.add(key)
        stack.extend(out.get(key, []))
    if not (reachable & ends):
        raise ValueError("START 到不了 END")
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

WORKFLOW_PROMPT = """你是这家公司的 CEO。请把下面这次任务沉淀成一张可复用的工作流。

准绳：
{anchor}

子任务与执行人：
{subtasks}

公司知识库（优先采用公司口径）：
{kb}

公开网络资料（仅作参考，注明不确定）：
{web}

历史版本经验（必须吸收，不要重复踩坑）：
{lessons}

要求：
- 只输出 JSON：{{"nodes":[{{"key":"start","type":"START","name":"开始"}},
  {{"key":"n1","type":"AGENT","name":"节点名","agent":"执行人姓名","instruction":"具体做什么、边界、交付标准","x":310,"y":120}},
  {{"key":"end","type":"END","name":"结束"}}],
  "edges":[{{"from":"start","to":"n1","label":""}},{{"from":"n1","to":"end","label":""}}]}}
- type 只能用 START/END/AGENT/APPROVAL/CONDITION/WAIT/TASK/PARALLEL/MERGE；
- 必须有 START 与 END，且从 START 能走到 END；
- 最多 {max_nodes} 个节点；能顺序执行就不要硬加分支；
- 每个 AGENT 节点必须写执行人姓名（与上面子任务里的执行人一致）和可照做的 instruction；
- 不要在 instruction 里编造数据来源；缺数据要写"先核实"。
"""


def _subtasks_block(mission: CeoMission) -> str:
    lines = []
    for i, item in enumerate(mission.plan or [], 1):
        skills = "、".join(str(s) for s in (item.get("required_skills") or []))
        lines.append(
            f"{i}. {item.get('title')}（执行人：{item.get('agent_name')}"
            f"{'；必备技能：' + skills if skills else ''}）\n"
            f"   {item.get('description') or ''}")
    return "\n".join(lines) or "（无）"


async def _chat_graph(db: AsyncSession, mission: CeoMission,
                      coordinator: Agent, context: dict,
                      lessons: list[str]) -> dict:
    from app import ceo_engine

    prompt = WORKFLOW_PROMPT.format(
        anchor=_anchor_summary(mission), subtasks=_subtasks_block(mission),
        kb=(context.get("kb_text") or "（无）")[:3500],
        web=(context.get("web_text") or "（无）")[:1500],
        lessons=("\n".join(f"- {x}" for x in lessons) or "（无）"),
        max_nodes=MAX_NODES)
    raw = await asyncio.wait_for(
        ceo_engine._chat(db, coordinator, [
            {"role": "system", "content": "你是企业流程设计师，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ], max_tokens=1800),
        timeout=AUTHOR_TIMEOUT_S)
    data = ceo_engine._extract_json(raw)
    return normalize_graph(data, mission, coordinator)


async def _persist_graph(db: AsyncSession, wf: WorkflowDefinition,
                         graph: dict) -> None:
    for node in graph["nodes"]:
        node_type = NodeType(node["type"])
        config: dict = {}
        for key in ("agent_id", "task_title_template", "instruction", "seconds",
                    "approver_hint", "keyword"):
            if key in node:
                config[key] = node[key]
        db.add(WorkflowNode(
            workflow_id=wf.id, node_key=node["key"], node_type=node_type,
            name=node["name"], config=config,
            position_x=float(node.get("x") or 0),
            position_y=float(node.get("y") or 0)))
    for edge in graph["edges"]:
        db.add(WorkflowEdge(workflow_id=wf.id, from_node=edge["from"],
                            to_node=edge["to"], label=edge.get("label") or None))
    await db.flush()


async def author_mission_workflow(db: AsyncSession, mission: CeoMission,
                                  coordinator: Agent | None) -> WorkflowDefinition | None:
    """为一场统筹生成工作流（同名任务复用高分公司已有版本）。

    失败返回 None，不向上抛：工作流是增强项，不能拖垮开工。
    """
    if not _env_on("AIEOS_CEO_WORKFLOW_AUTHOR", True):
        return None
    plan = list(mission.plan or [])
    if not plan:
        return None
    reuse_key = reuse_key_for(mission.title)
    signature = _plan_signature(plan)
    prior = await latest_workflow(db, mission.company_id, reuse_key)

    # 同一套拆解且上版已经跑得不错：直接复用，不再造一个同义版本
    if (prior is not None
            and (prior.meta or {}).get("signature") == signature
            and (prior.score is None or prior.score >= 90)):
        mission.workflow_id = prior.id
        await db.flush()
        return prior

    context = await gather_context(db, mission, coordinator)
    lessons = await collect_lessons(db, prior)
    graph: dict | None = None
    if _env_on("AIEOS_CEO_WORKFLOW_LLM", True) and coordinator is not None:
        try:
            graph = await _chat_graph(db, mission, coordinator, context, lessons)
        except Exception as exc:  # noqa: BLE001 — 模型图不合法就用确定性图
            logger.warning("工作流生成：模型图不可用，退到确定性图 mission=%s: %s",
                           mission.id, exc)
            context["errors"].append(f"模型图不可用：{type(exc).__name__}")
    used_llm = graph is not None
    if graph is None:
        graph = deterministic_graph(mission, coordinator)

    version = (prior.version + 1) if prior is not None else 1
    if prior is not None:
        prior.status = WorkflowStatus.ARCHIVED

    wf = WorkflowDefinition(
        company_id=mission.company_id, project_id=None,
        name=f"{mission.title}"[:300],
        description=f"由 CEO 依据准绳、知识库与网络调研生成。目标："
                    f"{mission.objective or mission.instruction}"[:2000],
        status=WorkflowStatus.PUBLISHED, created_by=mission.created_by,
        version=version, source="ceo",
        owner_agent_id=str(coordinator.id) if coordinator else None,
        reuse_key=reuse_key,
        previous_workflow_id=prior.id if prior is not None else None,
        meta={
            "signature": signature,
            "subtask_titles": [str(x.get("title") or "") for x in plan],
            "kb_hits": context.get("kb_hits", 0),
            "web_sources": context.get("web_sources") or [],
            "lessons": lessons,
            "errors": context.get("errors") or [],
            "generated_at": _iso(),
            "mission_id": mission.id,
        })
    db.add(wf)
    await db.flush()
    await _persist_graph(db, wf, graph)
    mission.workflow_id = wf.id
    await log_event(db, "workflow.generated", "workflow_definition", wf.id,
                    "agent", str(coordinator.id) if coordinator else None,
                    {"mission_id": mission.id, "version": version,
                     "nodes": len(graph["nodes"]),
                     "kb_hits": context.get("kb_hits", 0),
                     "web_sources": len(context.get("web_sources") or []),
                     "reused_from": prior.id if prior is not None else None,
                     "fallback": not used_llm},
                    company_id=mission.company_id)
    return wf


# ---------------------------------------------------------------------------
# 评分 / 反馈 / 升级
# ---------------------------------------------------------------------------

def score_mission(mission: CeoMission, results: list[dict] | None = None) -> dict:
    """确定性评分：完成率打底，偏离/失败/补跑分别扣分。"""
    stats = mission.stats or {}
    total = int(mission.subtask_count or stats.get("subtasks")
                or len(results or []) or len(mission.plan or []) or 1)
    completed = int(mission.completed_count if mission.completed_count is not None
                    else stats.get("completed") or 0)
    off_track = int(mission.off_track_count or stats.get("off_track") or 0)
    failed = int(mission.failed_count or stats.get("failed") or 0)
    retried = int(mission.retry_count or stats.get("retried") or 0)
    base = completed / max(1, total) * 100.0
    score = round(max(0.0, min(100.0, base - off_track * 15 - failed * 25
                               - retried * 5)), 1)
    success = mission.status == "COMPLETED" and score >= SUCCESS_THRESHOLD
    return {"score": score, "success": success,
            "metrics": {"subtasks": total, "completed": completed,
                        "off_track": off_track, "failed": failed,
                        "retried": retried}}


def lessons_from_results(results: list[dict] | None, limit: int = 8) -> list[str]:
    lessons: list[str] = []
    for r in results or []:
        status = str(r.get("status") or "")
        if status == "COMPLETED":
            continue
        title = str(r.get("title") or "未命名子任务")[:80]
        reason = str(r.get("off_track_reason") or r.get("error")
                     or status or "未完成")[:200]
        label = {"OFF_TRACK": "产出偏离", "SKIPPED": "被跳过"}.get(status, "执行失败")
        lessons.append(f"「{title}」{label}：{reason}")
        if len(lessons) >= limit:
            break
    return lessons


def _merge_meta(wf: WorkflowDefinition, **updates) -> None:
    meta = dict(wf.meta or {})
    meta.update(updates)
    wf.meta = meta


async def record_feedback(db: AsyncSession, workflow: WorkflowDefinition,
                          *, mission: CeoMission | None = None,
                          results: list[dict] | None = None,
                          success: bool | None = None,
                          score: float | None = None,
                          lessons: list[str] | None = None,
                          note: str | None = None,
                          source: str = "ceo",
                          created_by: str | None = None,
                          execution_id: str | None = None) -> WorkflowFeedback:
    """落一条反馈，并更新工作流的运行统计（最近一次分数 + 累计经验）。"""
    if mission is not None:
        scored = score_mission(mission, results)
        score = scored["score"] if score is None else score
        success = scored["success"] if success is None else success
        metrics = scored["metrics"]
        derived = lessons_from_results(results)
    else:
        score = float(score if score is not None else 0)
        success = bool(success)
        metrics = {}
        derived = []
    merged: list[str] = []
    for text in (lessons or []) + derived:
        text = str(text).strip()[:300]
        if text and text not in merged:
            merged.append(text)

    row = WorkflowFeedback(
        workflow_id=workflow.id, execution_id=execution_id,
        mission_id=mission.id if mission is not None else None,
        source=source, success=bool(success), score=float(score),
        metrics=metrics, lessons=merged[:20], note=(note or "")[:2000] or None,
        created_by=created_by)
    db.add(row)
    workflow.run_count += 1
    if success:
        workflow.success_count += 1
    else:
        workflow.failure_count += 1
    workflow.score = float(score)
    history = list((workflow.meta or {}).get("lessons") or [])
    for text in merged:
        if text not in history:
            history.append(text)
    _merge_meta(workflow, lessons=history[-20:], last_score=float(score),
                last_mission_id=mission.id if mission is not None else None,
                last_feedback_at=_iso())
    await db.flush()
    await log_event(db, "workflow.feedback_recorded", "workflow_definition",
                    workflow.id, "system", None,
                    {"mission_id": mission.id if mission is not None else None,
                     "score": float(score), "success": bool(success),
                     "lessons": len(merged)},
                    company_id=workflow.company_id)
    return row


def _lessons_block(lessons: list[str]) -> str:
    if not lessons:
        return ""
    lines = ["", "---", "历史运行经验（必须避免重复；遇到同类情况按经验处理）："]
    lines += [f"- {x}" for x in lessons[:10]]
    return "\n".join(lines)


async def optimize_workflow(db: AsyncSession, workflow: WorkflowDefinition,
                            lessons: list[str]) -> WorkflowDefinition | None:
    """把经验固化进新版本；旧版归档。版本上限内才升级。"""
    if not lessons or workflow.version >= MAX_VERSION:
        return None
    nodes = (await db.execute(
        select(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id)
        .order_by(WorkflowNode.created_at))).scalars().all()
    edges = (await db.execute(
        select(WorkflowEdge).where(WorkflowEdge.workflow_id == workflow.id)
        .order_by(WorkflowEdge.created_at))).scalars().all()
    if not nodes:
        return None

    workflow.status = WorkflowStatus.ARCHIVED
    meta = dict(workflow.meta or {})
    history = list(meta.get("lessons") or [])
    for text in lessons:
        if text not in history:
            history.append(text)
    new = WorkflowDefinition(
        company_id=workflow.company_id, project_id=workflow.project_id,
        name=workflow.name, description=workflow.description,
        status=WorkflowStatus.PUBLISHED, created_by=workflow.created_by,
        version=workflow.version + 1, source=workflow.source,
        owner_agent_id=workflow.owner_agent_id,
        reuse_key=workflow.reuse_key, previous_workflow_id=workflow.id,
        meta={**meta, "lessons": history[-20:], "optimized_from": workflow.id,
              "optimized_at": _iso()})
    db.add(new)
    await db.flush()

    for node in nodes:
        cfg = dict(node.config or {})
        if node.node_type == NodeType.AGENT:
            block = _lessons_block(lessons)
            instruction = str(cfg.get("instruction") or "")
            if block.strip() not in instruction:
                cfg["instruction"] = (instruction + block)[:4000]
        db.add(WorkflowNode(
            workflow_id=new.id, node_key=node.node_key,
            node_type=node.node_type, name=node.name, config=cfg,
            position_x=node.position_x, position_y=node.position_y))
    for edge in edges:
        db.add(WorkflowEdge(workflow_id=new.id, from_node=edge.from_node,
                            to_node=edge.to_node, label=edge.label))
    await db.flush()
    await log_event(db, "workflow.optimized", "workflow_definition", new.id,
                    "system", None,
                    {"from_version": workflow.version, "to_version": new.version,
                     "from_workflow_id": workflow.id,
                     "lessons": lessons[:10]},
                    company_id=workflow.company_id)
    return new


async def apply_mission_feedback(db: AsyncSession, mission: CeoMission,
                                 results: list[dict] | None) -> dict | None:
    """收尾时记录反馈；分数低于阈值就升一版。返回写入 mission.stats 的摘要。"""
    if not mission.workflow_id:
        return None
    workflow = await db.get(WorkflowDefinition, mission.workflow_id)
    if workflow is None:
        return None
    feedback = await record_feedback(db, workflow, mission=mission,
                                     results=results, source="ceo",
                                     created_by=mission.created_by)
    summary = {
        "workflow_id": workflow.id,
        "workflow_version": workflow.version,
        "workflow_score": feedback.score,
        "workflow_success": feedback.success,
    }
    if feedback.score < OPTIMIZE_THRESHOLD:
        lessons = list((workflow.meta or {}).get("lessons") or [])[-10:]
        optimized = await optimize_workflow(db, workflow, lessons)
        if optimized is not None:
            mission.workflow_id = optimized.id
            summary.update({
                "workflow_id": optimized.id,
                "workflow_version": optimized.version,
                "workflow_optimized_from": workflow.id,
            })
    return summary
