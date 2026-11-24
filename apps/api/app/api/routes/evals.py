"""Evaluation evidence API.

``/compare`` is intentionally manual/unverified. ``/run`` is server-owned: it
starts (or validates) a real workflow execution, runs one baseline model call,
then performs two independently recorded blind-review calls.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.api.deps import assert_company_capability, get_current_user
from app.background_jobs import QUEUE_FULL, submit_async
from app.core.permissions import Capability
from app.core.logging_config import get_logger
from app.db.session import AsyncSessionLocal, get_db
from app.eval.judge import VERDICTS, canonical_sha256, sha256_text
from app.eval.runner import (RUBRIC_VERSION, canonical_snapshot,
                             content_fingerprint, run_case)
from app.model_discovery import chat_model_condition
from app.model_gateway import ModelGateway, usage_context
from app.models.agent import Model
from app.models.eval import EvalRun
from app.models.task import Task, TaskStatus
from app.models.token_usage import TokenUsage
from app.models.workflow import (NodeType, RunStatus, WorkflowExecution,
                                 WorkflowStep)
from app.workflow_runtime import advance_execution, start_workflow

router = APIRouter(tags=["evals"])
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
TERMINAL_STATUSES = {"completed", "unavailable", "failed", "manual"}
EVAL_TIMEOUT_S = max(30.0, float(os.environ.get("AIEOS_EVAL_TIMEOUT_S", "900")))
LEASE_GRACE_S = 60.0
QUEUED_RECOVERY_GRACE = timedelta(minutes=5)
logger = get_logger(__name__)


class _EvalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalCompareIn(_EvalInput):
    company_id: str = Field(max_length=36)
    suite_key: str = Field(min_length=1, max_length=80)
    case_key: str = Field(min_length=1, max_length=80)
    multi_text: str
    baseline_text: str
    model_tier: str = Field(default="unknown", max_length=40)
    workflow_id: str | None = Field(default=None, max_length=36)
    multi_tokens: NonNegativeInt = 0
    baseline_tokens: NonNegativeInt = 0
    multi_latency_ms: NonNegativeInt = 0
    baseline_latency_ms: NonNegativeInt = 0
    notes: str | None = None


class EvalRunIn(_EvalInput):
    company_id: str = Field(max_length=36)
    suite_key: str = Field(min_length=1, max_length=80)
    case_key: str = Field(min_length=1, max_length=80)
    workflow_id: str = Field(max_length=36)
    prompt: str = Field(min_length=1)
    input_data: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = Field(default=None, max_length=36)
    model_tier: str = Field(default="configured", max_length=40)
    source_execution_id: str | None = Field(default=None, max_length=36)
    notes: str | None = None


def _enum_value(value) -> str | None:
    return str(getattr(value, "value", value)) if value is not None else None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:4000]


def _is_manual(r: EvalRun) -> bool:
    return r.run_kind == "manual" or (r.run_kind is None and r.evidence is None)


def _row_dict(r: EvalRun) -> dict:
    manual = _is_manual(r)
    status = r.status or ("manual" if manual else "unavailable")
    verification = r.verification or ("manual_unverified" if manual else "unavailable")
    evidence = r.evidence or {}
    blind = r.blind_evidence
    hashes = {
        "prompt": r.prompt_hash,
        "workflow_snapshot": r.workflow_snapshot_hash,
        "multi_output": r.multi_output_hash,
        "baseline_output": r.baseline_output_hash,
        "content_fingerprint": r.content_fingerprint,
    }
    source_path = None
    if r.source_execution_id and r.workflow_id:
        source_path = (f"/workflow-builder?open={r.workflow_id}"
                       f"&execution_id={r.source_execution_id}")
    return {
        "id": r.id, "company_id": r.company_id,
        "suite_key": r.suite_key, "case_key": r.case_key,
        "model_tier": r.model_tier, "workflow_id": r.workflow_id,
        "baseline_kind": r.baseline_kind,
        # Historical rows predate real blind review; never repeat their old
        # A_first marker as if it were evidence.
        "blind_order": r.blind_order if verification == "verified_blind" else None,
        "rubric": r.rubric, "scores": r.scores, "verdict": r.verdict,
        "judge": r.judge, "blind_evidence": blind,
        "tokens": r.tokens, "cost": r.cost, "latency_ms": r.latency_ms,
        "notes": r.notes, "created_at": r.created_at.isoformat() if r.created_at else None,
        "run_kind": r.run_kind or ("manual" if manual else "workflow"),
        "status": status, "verification": verification,
        "stage": evidence.get("stage"), "error": r.error,
        "source_execution_id": r.source_execution_id,
        "source_execution_path": source_path,
        "baseline_task_id": r.baseline_task_id,
        "hashes": hashes, "evidence": evidence,
        "rubric_version": r.rubric_version,
        "judge_model_id": r.judge_model_id,
        "judge_model_hash": r.judge_model_hash,
        "judge_prompt_hash": r.judge_prompt_hash,
        "idempotency_key": r.idempotency_key,
        "attempt": r.attempt, "retry_of": r.retry_of,
    }


async def _owned_workflow(db: AsyncSession, company_id: str, workflow_id: str):
    from app.models.workflow import WorkflowDefinition

    workflow = await db.get(WorkflowDefinition, workflow_id)
    if not workflow or workflow.company_id != company_id:
        raise HTTPException(404, "工作流不存在或不属于本公司")
    return workflow


def _safe_config(value):
    if isinstance(value, dict):
        blocked = {"api_key", "apikey", "secret", "password", "authorization",
                   "credential", "credentials", "headers", "token"}
        suffixes = ("_api_key", "_secret", "_password", "_token", "_key")
        result = {}
        for key, item in value.items():
            name = str(key).lower().replace("-", "_")
            if name == "last_test" or name.startswith("last_test_"):
                continue  # diagnostics are not model execution identity
            if name.endswith("_env") or name.endswith("_env_var"):
                allowed = True  # a key-variable reference is not a credential
            else:
                allowed = name not in blocked and not name.endswith(suffixes)
            if allowed:
                result[str(key)] = _safe_config(item)
        return result
    if isinstance(value, list):
        return [_safe_config(v) for v in value]
    return value


def _model_identity(model: Model) -> dict:
    """Hashable execution identity; credential values are never read or included."""
    return {
        "id": model.id, "company_id": model.company_id, "name": model.name,
        "provider_type": _enum_value(model.provider_type),
        # Endpoint and credential-variable identity determine what actually runs.
        # Values of credentials are never read or included.
        "base_url": model.base_url, "api_key_env": model.api_key_env,
        "model_id": model.model_id,
        "model_type": model.model_type, "config": _safe_config(model.config or {}),
    }


def _model_hash(model: Model) -> str:
    return canonical_sha256(_model_identity(model))


async def _resolve_model(db: AsyncSession, company_id: str,
                         model_id: str | None) -> Model:
    visible = or_(Model.company_id == company_id, Model.company_id.is_(None))
    conditions = [visible, Model.enabled.is_not(False), chat_model_condition()]
    if model_id:
        conditions.append(Model.id == model_id)
    q = select(Model).where(*conditions)
    if not model_id:
        q = q.order_by(case((Model.company_id == company_id, 0), else_=1),
                       Model.created_at, Model.id)
    model = (await db.execute(q.limit(1))).scalars().first()
    if not model:
        if model_id:
            raise HTTPException(404, "模型不存在、无权访问或不是可用的对话模型")
        raise HTTPException(503, detail={
            "status": "unavailable", "run": None,
            "message": "没有配置可用的对话模型，无法执行评测",
        })
    return model


def _source_prompt(execution: WorkflowExecution) -> str | None:
    data = execution.input_data or {}
    prompt = data.get("prompt")
    if isinstance(prompt, str):
        return prompt
    nested = data.get("input")
    if isinstance(nested, dict) and isinstance(nested.get("prompt"), str):
        return nested["prompt"]
    return None


def _idempotency_key(company_id: str, suite_key: str, case_key: str,
                     source_execution_id: str, fingerprint: str) -> str:
    return canonical_sha256({
        "company_id": company_id, "suite_key": suite_key, "case_key": case_key,
        "source_execution_id": source_execution_id,
        "content_fingerprint": fingerprint,
    })


async def _insert_run_idempotent(db: AsyncSession, run: EvalRun) -> tuple[EvalRun, bool]:
    """Insert with a savepoint; a racing duplicate reloads the committed winner."""
    try:
        async with db.begin_nested():
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = (await db.execute(select(EvalRun).where(
            EvalRun.idempotency_key == run.idempotency_key))).scalars().first()
        await db.commit()
        if existing is None:
            raise HTTPException(409, "评测幂等键冲突，且无法读取已有记录")
        return existing, False
    await db.commit()
    await db.refresh(run)
    return run, True


@router.post("/evals/compare", status_code=201)
async def record_compare(data: EvalCompareIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """Record pasted outputs for deterministic rules only; never call this blind review."""
    await assert_company_capability(db, user.id, data.company_id, Capability.OPERATE)
    if data.workflow_id:
        await _owned_workflow(db, data.company_id, data.workflow_id)
    result = await run_case({
        "key": data.case_key, "model_tier": data.model_tier,
        "multi": data.multi_text, "baseline": data.baseline_text,
        "multi_tokens": data.multi_tokens, "baseline_tokens": data.baseline_tokens,
        "multi_latency_ms": data.multi_latency_ms,
        "baseline_latency_ms": data.baseline_latency_ms,
    })
    if result["verdict"] not in VERDICTS:
        raise HTTPException(500, "verdict 计算异常")
    run = EvalRun(
        company_id=data.company_id, suite_key=data.suite_key, case_key=data.case_key,
        model_tier=data.model_tier, workflow_id=data.workflow_id,
        baseline_kind="manual_pasted", blind_order="none",
        rubric=result["scores"]["multi"]["weights"],
        scores={
            "multi": result["scores"]["multi"]["total"],
            "baseline": result["scores"]["baseline"]["total"],
            "dims": {"multi": result["scores"]["multi"]["dims"],
                     "baseline": result["scores"]["baseline"]["dims"]},
            "multi_tokens": data.multi_tokens, "baseline_tokens": data.baseline_tokens,
            "multi_latency_ms": data.multi_latency_ms,
            "baseline_latency_ms": data.baseline_latency_ms,
        },
        verdict=result["verdict"], judge=result["judge"],
        blind_evidence=result["blind"], tokens=data.multi_tokens + data.baseline_tokens,
        latency_ms=max(data.multi_latency_ms, data.baseline_latency_ms),
        notes=data.notes, created_by=user.id, run_kind="manual", status="manual",
        verification="manual_unverified", rubric_version=RUBRIC_VERSION,
        prompt_hash=result["hashes"]["prompt"],
        multi_output_hash=result["hashes"]["multi_output"],
        baseline_output_hash=result["hashes"]["baseline_output"],
        evidence={"stage": "manual", "verification": "manual_unverified",
                  "notice": "pasted outputs; deterministic rules only; no blind review",
                  "input_metrics": {
                      "multi_tokens": data.multi_tokens,
                      "baseline_tokens": data.baseline_tokens,
                      "multi_latency_ms": data.multi_latency_ms,
                      "baseline_latency_ms": data.baseline_latency_ms,
                  }},
    )
    db.add(run)
    await db.commit()
    return _row_dict(run)


@router.post("/evals/run")
async def launch_eval_run(data: EvalRunIn, response: Response,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """Launch a real source workflow + baseline + dual-order blind review.

    Supplying an existing completed ``source_execution_id`` makes retries
    idempotent. Without one, the server creates a new source execution, whose
    identity becomes part of the unique idempotency key.
    """
    await assert_company_capability(db, user.id, data.company_id, Capability.OPERATE)
    workflow = await _owned_workflow(db, data.company_id, data.workflow_id)
    model = await _resolve_model(db, data.company_id, data.model_id)
    identity = _model_identity(model)
    model_hash = _model_hash(model)

    if data.source_execution_id:
        execution = await db.get(WorkflowExecution, data.source_execution_id)
        if not execution or execution.workflow_id != workflow.id:
            raise HTTPException(404, "源执行不存在或不属于所选工作流")
        if execution.status != RunStatus.COMPLETED:
            raise HTTPException(409, detail={
                "status": "unavailable", "run": None,
                "message": "源工作流执行尚未完成，不能作为评测证据",
            })
        source_prompt = _source_prompt(execution)
        if source_prompt != data.prompt:
            raise HTTPException(409, detail={
                "status": "unavailable", "run": None,
                "message": "评测 prompt 与源执行输入不一致",
            })
        if execution.definition_snapshot is None:
            raise HTTPException(409, detail={
                "status": "unavailable", "run": None,
                "message": "源执行缺少定义快照，无法固定评测输入",
            })
    else:
        # Runtime interpolation for {{input.prompt}} reads top-level input_data.
        source_input = {**data.input_data, "prompt": data.prompt}
        try:
            execution = await start_workflow(
                db, workflow.id, user.id, project_id=workflow.project_id,
                input_data=source_input)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    snapshot = canonical_snapshot(execution.definition_snapshot)
    fingerprint = content_fingerprint(
        prompt=data.prompt, source_input_data=execution.input_data,
        workflow_snapshot=snapshot,
        model_identity=identity, rubric_version=RUBRIC_VERSION)
    key = _idempotency_key(data.company_id, data.suite_key, data.case_key,
                           execution.id, fingerprint)
    existing = (await db.execute(select(EvalRun).where(
        EvalRun.idempotency_key == key))).scalars().first()
    if existing:
        await db.commit()
        return _row_dict(existing)

    run = EvalRun(
        company_id=data.company_id, suite_key=data.suite_key, case_key=data.case_key,
        model_tier=data.model_tier, workflow_id=workflow.id,
        baseline_kind="single_agent", blind_order="none", rubric={}, scores={},
        verdict="unavailable", judge=None, tokens=0, cost=0, latency_ms=0,
        notes=data.notes, created_by=user.id, run_kind="workflow", status="queued",
        verification="pending", source_execution_id=execution.id,
        attempt=1, retry_of=None,
        prompt_hash=sha256_text(data.prompt),
        workflow_snapshot_hash=canonical_sha256(snapshot),
        content_fingerprint=fingerprint, idempotency_key=key,
        rubric_version=RUBRIC_VERSION, judge_model_id=model.id,
        judge_model_hash=model_hash,
        blind_evidence={"available": False, "status": "queued", "mode": "dual_order",
                        "order1": None, "order2": None, "agreed": None, "orders": []},
        evidence={
            "schema_version": 1, "stage": "queued", "model": identity,
            "model_hash": model_hash, "source_execution_id": execution.id,
            "source_status": _enum_value(execution.status),
            "source_input_data": execution.input_data,
            "workflow_snapshot": snapshot, "source_steps": [],
            "source_task_ids": [], "usage": {},
        },
    )
    run, created = await _insert_run_idempotent(db, run)
    if not created:
        return _row_dict(run)

    await _submit_worker(db, run)
    response.status_code = 202
    return _row_dict(run)


async def _submit_worker(db: AsyncSession, run: EvalRun) -> None:
    """Submit once; rejected/crashed submissions become retryable unavailable rows."""
    try:
        job = submit_async(lambda session: _execute_eval(session, run.id),
                           timeout_s=EVAL_TIMEOUT_S,
                           on_timeout=lambda session, timeout: _mark_timeout(
                               session, run.id, timeout),
                           kind="eval")
    except Exception as exc:
        await _mark_unavailable(db, run, "queue_submit_failed", _error_text(exc),
                                expected_status="queued")
        return
    if job is QUEUE_FULL:
        # Leave the PENDING source execution intact: retry uses that same input
        # and snapshot instead of silently replacing the evidence source.
        await _mark_unavailable(db, run, "queue_full", "评测队列已满，请稍后重试",
                                expected_status="queued")


async def _retry_key(original: EvalRun) -> str:
    return canonical_sha256({"retry_of": original.id, "attempt": (original.attempt or 1) + 1})


@router.post("/evals/{rid}/retry", status_code=202)
async def retry_eval(rid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """Create a new immutable attempt from an unavailable/failed evaluation."""
    original = await db.get(EvalRun, rid)
    if not original:
        raise HTTPException(404, "Eval run not found")
    await assert_company_capability(db, user.id, original.company_id, Capability.OPERATE)
    if original.run_kind != "workflow" or original.status not in {"unavailable", "failed"}:
        raise HTTPException(409, "只有不可用或失败的真实评测可以重试")
    execution = await db.get(WorkflowExecution, original.source_execution_id or "")
    if not execution or execution.status not in {RunStatus.COMPLETED, RunStatus.PENDING}:
        raise HTTPException(409, "重试需要已完成或待执行的源工作流执行")
    model = await _resolve_model(db, original.company_id, original.judge_model_id)
    identity = _model_identity(model)
    model_hash = _model_hash(model)
    snapshot = canonical_snapshot(execution.definition_snapshot)
    fingerprint = content_fingerprint(
        prompt=_source_prompt(execution) or "", source_input_data=execution.input_data,
        workflow_snapshot=snapshot, model_identity=identity,
        rubric_version=original.rubric_version or RUBRIC_VERSION)
    run = EvalRun(
        company_id=original.company_id, suite_key=original.suite_key,
        case_key=original.case_key, model_tier=original.model_tier,
        workflow_id=original.workflow_id, baseline_kind=original.baseline_kind,
        blind_order="none", rubric={}, scores={}, verdict="unavailable", judge=None,
        tokens=0, cost=0, latency_ms=0, notes=original.notes, created_by=user.id,
        run_kind="workflow", status="queued", verification="pending",
        source_execution_id=execution.id, attempt=(original.attempt or 1) + 1,
        retry_of=original.id, prompt_hash=original.prompt_hash,
        workflow_snapshot_hash=canonical_sha256(snapshot),
        content_fingerprint=fingerprint, idempotency_key=await _retry_key(original),
        rubric_version=original.rubric_version or RUBRIC_VERSION,
        judge_model_id=model.id, judge_model_hash=model_hash,
        blind_evidence={"available": False, "status": "queued", "mode": "dual_order",
                        "order1": None, "order2": None, "agreed": None, "orders": []},
        evidence={"schema_version": 1, "stage": "queued", "model": identity,
                  "model_hash": model_hash, "source_execution_id": execution.id,
                  "source_status": _enum_value(execution.status),
                  "source_input_data": execution.input_data,
                  "workflow_snapshot": snapshot, "source_steps": [],
                  "source_task_ids": [], "usage": {}, "retry_of": original.id},
    )
    run, created = await _insert_run_idempotent(db, run)
    if created:
        await _submit_worker(db, run)
    return _row_dict(run)


async def _usage_rows(db: AsyncSession, task_ids: list[str]) -> list[dict]:
    if not task_ids:
        return []
    rows = (await db.execute(select(TokenUsage).where(
        TokenUsage.task_id.in_(task_ids)).order_by(TokenUsage.created_at))).scalars().all()
    return [{
        "id": row.id, "task_id": row.task_id, "scenario": row.scenario,
        "model_id": row.model_id, "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens, "total_tokens": row.total_tokens,
        "total_cost": row.total_cost, "latency_ms": row.latency_ms,
        "success": row.success,
    } for row in rows]


def _usage_totals(rows: list[dict]) -> dict:
    return {
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
        "cost": round(sum(float(r.get("total_cost") or 0) for r in rows), 8),
        "latency_ms": sum(int(r.get("latency_ms") or 0) for r in rows),
    }


def _reported_usage(results: list[dict], fallback_rows: list[dict]) -> dict:
    values = []
    for result in results:
        usage = result.get("usage") or {}
        inp = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        out = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        total = int(usage.get("total_tokens", inp + out) or 0)
        values.append({"input_tokens": inp, "output_tokens": out,
                       "total_tokens": total, "total_cost": 0, "latency_ms": 0})
    if not any(v["total_tokens"] for v in values):
        return _usage_totals(fallback_rows)
    return _usage_totals(values)


def _terminal_agent_step(execution: WorkflowExecution, steps: list[WorkflowStep]):
    """Select the newest usable AGENT on a path to the actual terminal END.

    Completion timestamps are not graph order: a late side branch must never
    replace the output of the branch that reached the terminal node.
    """
    snapshot = execution.definition_snapshot or {}
    ends = set(execution.current_nodes or [])
    completed_ends = {s.node_key for s in steps
                      if s.node_type == NodeType.END and s.status == RunStatus.COMPLETED}
    ends &= completed_ends
    if not ends:
        ends = {n.get("node_key") for n in snapshot.get("nodes", [])
                if n.get("node_type") == NodeType.END.value} & completed_ends
    if not ends:
        return None
    predecessors: dict[str, set[str]] = {}
    for edge in snapshot.get("edges", []):
        predecessors.setdefault(edge.get("to_node"), set()).add(edge.get("from_node"))
    distance = {node: 0 for node in ends}
    queue = list(ends)
    while queue:
        node = queue.pop(0)
        for pred in predecessors.get(node, set()):
            if pred not in distance:
                distance[pred] = distance[node] + 1
                queue.append(pred)
    candidates = [s for s in steps
                  if s.node_type == NodeType.AGENT and s.status == RunStatus.COMPLETED
                  and not s.stale_at and s.node_key in distance
                  and isinstance((s.output_data or {}).get("output"), str)]
    if not candidates:
        return None
    # Latest attempt at the nearest terminal ancestor; node key breaks ties
    # deterministically when a merge has multiple equally near AGENT branches.
    return min(candidates, key=lambda s: (distance[s.node_key], -s.attempt, s.node_key))


async def _source_evidence(db: AsyncSession, execution: WorkflowExecution) -> tuple[dict, str | None]:
    steps = (await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == execution.id).order_by(
            WorkflowStep.completed_at, WorkflowStep.created_at,
            WorkflowStep.node_key, WorkflowStep.attempt))).scalars().all()
    records: list[dict] = []
    task_ids: list[str] = []
    for step in steps:
        output = step.output_data if isinstance(step.output_data, dict) else {}
        task_id = output.get("task_id")
        if isinstance(task_id, str) and task_id:
            task_ids.append(task_id)
        records.append({
            "identity": step.id,
            "id": step.id, "execution_id": execution.id,
            "node_key": step.node_key, "attempt": step.attempt,
            "node_type": _enum_value(step.node_type), "status": _enum_value(step.status),
            "task_id": task_id, "output_hash": canonical_sha256(output),
            "input_signature": step.input_signature, "stale_at": step.stale_at,
        })
    terminal = _terminal_agent_step(execution, steps)
    multi_text = terminal.output_data.get("output") if terminal else None
    return ({
        "source_execution_id": execution.id,
        "source_status": _enum_value(execution.status),
        "source_error": execution.error,
        "source_output_node": terminal.node_key if terminal else None,
        "source_output_step_id": terminal.id if terminal else None,
        "source_steps": records, "source_task_ids": list(dict.fromkeys(task_ids)),
    }, multi_text)


def _lease_expiry() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        seconds=EVAL_TIMEOUT_S + LEASE_GRACE_S)


async def _claim_run(db: AsyncSession, eval_id: str) -> str | None:
    """Atomic queued/expired-lease claim; only one worker can own an attempt."""
    worker_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(update(EvalRun).where(
        EvalRun.id == eval_id,
        or_(EvalRun.status == "queued",
            and_(EvalRun.status == "running", EvalRun.lease_expires_at <= now)),
        or_(EvalRun.worker_id.is_(None), EvalRun.lease_expires_at <= now),
    ).values(status="running", worker_id=worker_id, lease_expires_at=_lease_expiry())
        .execution_options(synchronize_session=False))
    await db.commit()
    return worker_id if result.rowcount == 1 else None


async def _cas_active(db: AsyncSession, run: EvalRun, **values) -> bool:
    """Write only while this worker still owns a non-terminal attempt."""
    result = await db.execute(update(EvalRun).where(
        EvalRun.id == run.id, EvalRun.worker_id == run.worker_id,
        EvalRun.status.notin_(TERMINAL_STATUSES),
    ).values(**values).execution_options(synchronize_session=False))
    await db.commit()
    if result.rowcount != 1:
        await db.refresh(run)
        return False
    for key, value in values.items():
        set_committed_value(run, key, value)
    return True


async def _terminal_update(db: AsyncSession, run: EvalRun, *, status: str,
                           verification: str, reason: str, message: str | None,
                           evidence: dict, blind: dict, values: dict | None = None,
                           expected_status: str | None = None,
                           expected_worker_id: str | None = None,
                           lease_expires_at_lte: datetime | None = None) -> bool:
    """CAS terminal evidence exactly once; late workers cannot overwrite it."""
    conditions = [EvalRun.id == run.id, EvalRun.status.notin_(TERMINAL_STATUSES),
                  EvalRun.worker_id == run.worker_id]
    if expected_status is not None:
        conditions.append(EvalRun.status == expected_status)
    if expected_worker_id is not None:
        conditions.append(EvalRun.worker_id == expected_worker_id)
    if lease_expires_at_lte is not None:
        conditions.append(EvalRun.lease_expires_at <= lease_expires_at_lte)
    payload = {
        **(values or {}), "status": status, "verification": verification,
        "verdict": "unavailable", "error": message or reason,
        "blind_order": "none", "evidence": evidence, "blind_evidence": blind,
        "worker_id": None, "lease_expires_at": None,
    }
    result = await db.execute(update(EvalRun).where(*conditions).values(
        **payload).execution_options(synchronize_session=False))
    await db.commit()
    await db.refresh(run)
    return result.rowcount == 1


async def _set_stage(db: AsyncSession, run: EvalRun, stage: str) -> bool:
    return await _cas_active(db, run, status="running",
                             evidence={**(run.evidence or {}), "stage": stage},
                             lease_expires_at=_lease_expiry())


async def _mark_unavailable(db: AsyncSession, run: EvalRun, reason: str,
                            message: str | None = None, *,
                            expected_status: str | None = None,
                            expected_worker_id: str | None = None,
                            lease_expires_at_lte: datetime | None = None,
                            values: dict | None = None) -> bool:
    evidence = {**(run.evidence or {}), "stage": "unavailable", "reason": reason}
    blind = run.blind_evidence
    if not blind or blind.get("status") == "queued":
        blind = {"available": False, "status": "unavailable", "mode": "dual_order",
                 "order1": None, "order2": None, "agreed": None, "orders": [],
                 "reason": reason}
    else:
        blind = {**blind, "available": False, "status": "unavailable", "reason": reason}
    return await _terminal_update(db, run, status="unavailable",
                                  verification="unavailable", reason=reason,
                                  message=message, evidence=evidence, blind=blind,
                                  values=values, expected_status=expected_status,
                                  expected_worker_id=expected_worker_id,
                                  lease_expires_at_lte=lease_expires_at_lte)


async def _mark_failed(db: AsyncSession, run: EvalRun, exc: Exception) -> bool:
    evidence = {**(run.evidence or {}), "stage": "failed", "reason": "execution_failed"}
    return await _terminal_update(db, run, status="failed",
                                  verification="unavailable", reason="execution_failed",
                                  message=_error_text(exc), evidence=evidence,
                                  blind={"available": False, "status": "unavailable",
                                         "mode": "dual_order", "order1": None,
                                         "order2": None, "agreed": None, "orders": [],
                                         "reason": "execution_failed"})


async def _mark_timeout(db: AsyncSession, eval_id: str, timeout_s: float) -> None:
    run = await db.get(EvalRun, eval_id)
    if not run or run.status != "running" or run.lease_expires_at is None:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lease = run.lease_expires_at
    if lease.tzinfo is not None:
        lease = lease.replace(tzinfo=None)
    if lease > now:
        return
    # An expired worker may still return later, but its final ownership CAS loses.
    await _mark_unavailable(db, run, "timeout",
                            f"eval timed out after {timeout_s:.0f}s",
                            expected_status="running",
                            expected_worker_id=run.worker_id,
                            lease_expires_at_lte=now)


async def recover_interrupted_evals(*, reason: str = "startup_interrupted") -> int:
    """Recover orphaned submissions/workers after startup or a missed worker."""
    async with AsyncSessionLocal() as db:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = (await db.execute(select(EvalRun).where(
            EvalRun.run_kind == "workflow", EvalRun.status.notin_(TERMINAL_STATUSES),
        ))).scalars().all()
        recovered = 0
        for run in rows:
            lease = run.lease_expires_at
            if lease is not None and lease.tzinfo is not None:
                lease = lease.replace(tzinfo=None)
            # A newly started process can coexist with the process that submitted
            # this row. Give fresh queued work time to be claimed before treating
            # it as orphaned; running work is protected by its ownership lease.
            created = run.created_at
            if created is not None and created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            queued_stale = (run.status == "queued" and
                            (created is None or created <= cutoff - QUEUED_RECOVERY_GRACE))
            running_stale = (run.status == "running" and lease is not None
                             and lease <= cutoff)
            stale = queued_stale or running_stale
            if not stale:
                continue
            message = ("服务重启中断了评测，请重试" if reason == "startup_interrupted"
                       else "评测 worker 长时间未领取或已失联，请重试")
            if await _mark_unavailable(db, run, reason, message,
                                       expected_status=run.status,
                                       expected_worker_id=run.worker_id,
                                       lease_expires_at_lte=cutoff
                                       if running_stale else None):
                recovered += 1
        return recovered


async def run_periodic_eval_recovery(interval_seconds: float = 60) -> None:
    """Close the gap where fresh queued work outlives the process that submitted it."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            recovered = await recover_interrupted_evals(reason="orphaned_worker")
            if recovered:
                logger.info("[eval-recovery] 恢复失联评测 %d 条为不可用", recovered)
        except Exception as exc:  # noqa: BLE001 - 单次自愈失败不能终止周期任务
            logger.error("[eval-recovery] 周期恢复失败：%s", exc, exc_info=True)


async def _execute_eval(db: AsyncSession, eval_id: str) -> None:
    """Claim atomically, then publish each stage through ownership-checked CAS."""
    run = await db.get(EvalRun, eval_id)
    if not run or run.status in TERMINAL_STATUSES:
        return
    worker_id = await _claim_run(db, eval_id)
    if worker_id is None:
        return
    await db.refresh(run)
    run.worker_id = worker_id
    try:
        if not await _set_stage(db, run, "source"):
            return
        execution = await db.get(WorkflowExecution, run.source_execution_id or "")
        if not execution:
            await _mark_unavailable(db, run, "source_execution_missing")
            return
        if execution.status != RunStatus.COMPLETED:
            execution = await advance_execution(db, execution.id, max_steps=50)
            await db.refresh(execution)
        source, multi_text = await _source_evidence(db, execution)
        source["usage"] = await _usage_rows(db, source["source_task_ids"])
        if not await _cas_active(db, run, evidence={**(run.evidence or {}), **source},
                                 lease_expires_at=_lease_expiry()):
            return
        if execution.status != RunStatus.COMPLETED:
            detail = f"：{execution.error}" if execution.error else ""
            await _mark_unavailable(db, run, "source_not_completed",
                                    f"源工作流状态为 {_enum_value(execution.status)}，评测不可用{detail}")
            return
        if multi_text is None:
            await _mark_unavailable(db, run, "source_output_missing",
                                    "源工作流没有可用的 AGENT 产出")
            return

        try:
            model = await _resolve_model(db, run.company_id, run.judge_model_id)
        except HTTPException as exc:
            await _mark_unavailable(db, run, "model_unavailable",
                                    str(exc.detail.get("message") if isinstance(exc.detail, dict)
                                        else exc.detail))
            return
        actual_model_hash = _model_hash(model)
        if actual_model_hash != run.judge_model_hash:
            await _mark_unavailable(db, run, "model_configuration_changed",
                                    "模型配置在评测启动后发生变化，请重新发起")
            return

        prompt = _source_prompt(execution)
        if prompt is None or sha256_text(prompt) != run.prompt_hash:
            await _mark_unavailable(db, run, "source_prompt_mismatch")
            return
        if canonical_sha256(canonical_snapshot(execution.definition_snapshot)) != run.workflow_snapshot_hash:
            await _mark_unavailable(db, run, "source_snapshot_mismatch")
            return
        source_fingerprint = content_fingerprint(
            prompt=prompt, source_input_data=execution.input_data,
            workflow_snapshot=execution.definition_snapshot,
            model_identity=_model_identity(model),
            rubric_version=run.rubric_version or RUBRIC_VERSION)
        if source_fingerprint != run.content_fingerprint:
            await _mark_unavailable(db, run, "source_input_mismatch")
            return

        if not await _set_stage(db, run, "baseline"):
            return
        baseline_task = Task(
            company_id=run.company_id, project_id=execution.project_id,
            title=f"评测基线：{run.case_key}"[:500], description=prompt,
            status=TaskStatus.RUNNING, created_by=run.created_by,
            started_at=_utcnow_iso(), progress=0.5, current_stage="eval_baseline")
        db.add(baseline_task)
        await db.commit()
        if not await _cas_active(db, run, baseline_task_id=baseline_task.id,
                                 evidence={**(run.evidence or {}),
                                           "baseline_task_id": baseline_task.id},
                                 lease_expires_at=_lease_expiry()):
            baseline_task.status = TaskStatus.FAILED
            baseline_task.current_stage = "eval_worker_lost"
            await db.commit()
            return
        gateway = ModelGateway(db)
        t0 = time.perf_counter()
        try:
            with usage_context(
                    company_id=run.company_id, project_id=execution.project_id,
                    user_id=run.created_by, task_id=baseline_task.id, scenario="EVAL",
                    detail={"eval_run_id": run.id, "role": "baseline"}):
                baseline_result = await gateway.chat_with_agent_models(
                    [model.id], [{"role": "user", "content": prompt}])
            baseline_text = baseline_result.get("content")
            if not isinstance(baseline_text, str):
                raise ValueError("baseline model returned no text content")
        except Exception as exc:
            await db.commit()  # persist failed-call metering before terminal state
            baseline_task.status = TaskStatus.FAILED
            baseline_task.current_stage = "eval_baseline_failed"
            await db.commit()
            await _mark_unavailable(db, run, "baseline_unavailable", _error_text(exc))
            return
        baseline_latency = max(0, int((time.perf_counter() - t0) * 1000))
        baseline_task.status = TaskStatus.COMPLETED
        baseline_task.result = baseline_text
        baseline_task.progress = 1.0
        baseline_task.current_stage = "eval_baseline_completed"
        baseline_task.completed_at = _utcnow_iso()
        await db.commit()

        if not await _set_stage(db, run, "judging"):
            return
        judge_task = Task(
            company_id=run.company_id, project_id=execution.project_id,
            title=f"评测盲评：{run.case_key}"[:500],
            description="dual-order blind review", status=TaskStatus.RUNNING,
            created_by=run.created_by, started_at=_utcnow_iso(), progress=0.5,
            current_stage="eval_judging")
        db.add(judge_task)
        await db.commit()
        if not await _cas_active(db, run,
                                 evidence={**(run.evidence or {}),
                                           "judge_task_id": judge_task.id},
                                 lease_expires_at=_lease_expiry()):
            judge_task.status = TaskStatus.FAILED
            judge_task.current_stage = "eval_worker_lost"
            await db.commit()
            return
        judge_results: list[dict] = []
        judge_started = time.perf_counter()

        async def _judge_call(judge_prompt: str):
            try:
                with usage_context(
                        company_id=run.company_id, project_id=execution.project_id,
                        user_id=run.created_by, task_id=judge_task.id, scenario="EVAL",
                        detail={"eval_run_id": run.id, "role": "judge"}):
                    result = await gateway.chat_with_agent_models(
                        [model.id], [{"role": "user", "content": judge_prompt}])
                judge_results.append(result)
                return result.get("content")
            finally:
                await db.commit()

        result = await run_case({
            "key": run.case_key, "model_tier": run.model_tier,
            "multi": multi_text, "baseline": baseline_text, "prompt": prompt,
            "workflow_snapshot": canonical_snapshot(execution.definition_snapshot),
            "source_input_data": execution.input_data,
            "model_identity": _model_identity(model),
            "rubric_version": run.rubric_version or RUBRIC_VERSION,
            "multi_tokens": 0, "baseline_tokens": 0,
            "multi_latency_ms": 0, "baseline_latency_ms": baseline_latency,
        }, judge_fn=_judge_call)
        judge_task.status = TaskStatus.COMPLETED
        judge_task.result = "dual-order review recorded"
        judge_task.progress = 1.0
        judge_task.current_stage = "eval_judging_completed"
        judge_task.completed_at = _utcnow_iso()

        source_ids = run.evidence.get("source_task_ids") or []
        all_rows = await _usage_rows(db, [*source_ids, baseline_task.id, judge_task.id])
        source_rows = [r for r in all_rows if r["task_id"] in source_ids]
        baseline_rows = [r for r in all_rows if r["task_id"] == baseline_task.id]
        judge_rows = [r for r in all_rows if r["task_id"] == judge_task.id]
        source_usage = _usage_totals(source_rows)
        baseline_usage = _reported_usage([baseline_result], baseline_rows)
        baseline_usage["cost"] = _usage_totals(baseline_rows)["cost"]
        baseline_usage["latency_ms"] = baseline_latency
        judge_usage = _reported_usage(judge_results, judge_rows)
        judge_usage["cost"] = _usage_totals(judge_rows)["cost"]
        # Gateway result latency is not uniform across providers; persisted
        # metering is authoritative when present, measured wall time otherwise.
        judge_usage["latency_ms"] = (sum(int(r.get("latency_ms") or 0) for r in judge_rows)
                                     or max(0, int((time.perf_counter() - judge_started) * 1000)))
        total_usage = _usage_totals([
            {**source_usage, "total_cost": source_usage["cost"]},
            {**baseline_usage, "total_cost": baseline_usage["cost"]},
            {**judge_usage, "total_cost": judge_usage["cost"]},
        ])
        final_evidence = {
            **(run.evidence or {}),
            "stage": "unavailable" if result["verdict"] == "unavailable" else "completed",
            "outputs": {"multi": multi_text, "baseline": baseline_text},
            "usage": {"source": source_usage, "baseline": baseline_usage,
                      "judge": judge_usage, "total": total_usage, "persisted_rows": all_rows},
            "rubric": result["scores"],
        }
        if result["verdict"] == "unavailable":
            final_evidence["reason"] = result["blind"]["reason"]
        final_values = {
            "rubric": result["scores"]["multi"]["weights"],
            "scores": {
                "multi": result["scores"]["multi"]["total"],
                "baseline": result["scores"]["baseline"]["total"],
                "dims": {"multi": result["scores"]["multi"]["dims"],
                         "baseline": result["scores"]["baseline"]["dims"]},
                "multi_tokens": source_usage["total_tokens"],
                "baseline_tokens": baseline_usage["total_tokens"],
                "multi_latency_ms": source_usage["latency_ms"],
                "baseline_latency_ms": baseline_latency,
            },
            "verdict": result["verdict"], "blind_evidence": result["blind"],
            "judge": {**result["judge"], "model_id": model.id,
                      "model_hash": actual_model_hash,
                      "prompt_hashes": [o["prompt_hash"] for o in result["blind"]["orders"]]},
            "tokens": total_usage["total_tokens"], "cost": total_usage["cost"],
            "latency_ms": max(source_usage["latency_ms"], baseline_latency),
            "prompt_hash": result["hashes"]["prompt"],
            "workflow_snapshot_hash": result["hashes"]["workflow_snapshot"],
            "multi_output_hash": result["hashes"]["multi_output"],
            "baseline_output_hash": result["hashes"]["baseline_output"],
            "content_fingerprint": result["content_fingerprint"],
            "rubric_version": result["rubric_version"],
            "judge_model_id": model.id, "judge_model_hash": actual_model_hash,
            "judge_prompt_hash": result["judge"]["prompt_hash"],
            "blind_order": "A_first" if result["blind"]["available"] else "none",
            "evidence": final_evidence,
        }
        judge_task.status = TaskStatus.COMPLETED
        judge_task.result = "dual-order review recorded"
        judge_task.progress = 1.0
        judge_task.current_stage = "eval_judging_completed"
        judge_task.completed_at = _utcnow_iso()
        await db.commit()
        if result["verdict"] == "unavailable":
            await _terminal_update(db, run, status="unavailable",
                                   verification="unavailable",
                                   reason=result["blind"]["reason"],
                                   message=result["blind"]["reason"],
                                   evidence=final_evidence,
                                   blind=result["blind"], values=final_values)
        else:
            await _cas_active(db, run, status="completed",
                              verification="verified_blind", error=None,
                              worker_id=None, lease_expires_at=None, **final_values)
    except Exception as exc:
        await db.rollback()
        run = await db.get(EvalRun, eval_id)
        if run and run.status not in {"completed", "unavailable", "failed", "manual"}:
            await _mark_failed(db, run, exc)
        else:
            raise


async def _suite_rows(db: AsyncSession, company_id: str,
                      suite_key: str) -> list[EvalRun]:
    q = await db.execute(select(EvalRun).where(
        EvalRun.company_id == company_id, EvalRun.suite_key == suite_key)
        .order_by(EvalRun.created_at))
    return list(q.scalars())


@router.get("/evals")
async def list_evals(company_id: str, suite_key: str | None = None,
                     limit: int = Query(default=50, ge=1, le=200),
                     offset: int = Query(default=0, ge=0),
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    conditions = [EvalRun.company_id == company_id]
    if suite_key:
        conditions.append(EvalRun.suite_key == suite_key)
    total = (await db.execute(select(func.count()).select_from(EvalRun).where(
        *conditions))).scalar_one()
    rows = (await db.execute(select(EvalRun).where(*conditions).order_by(
        EvalRun.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [_row_dict(r) for r in rows], "total": total}


@router.get("/evals/summary")
async def eval_summary(company_id: str, suite_key: str,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """Keep verified blind outcomes separate from manual rules-only records."""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = await _suite_rows(db, company_id, suite_key)
    by_tier: dict[str, dict] = {}
    for r in rows:
        t = by_tier.setdefault(r.model_tier, {
            "cases": 0, "multi_better": 0, "tie": 0, "baseline_better": 0,
            "unavailable": 0, "verified_cases": 0, "manual_cases": 0,
            "unavailable_cases": 0, "verified_multi_better": 0,
            "verified_baseline_better": 0, "manual_multi_better": 0,
            "manual_baseline_better": 0, "multi_tokens": 0, "baseline_tokens": 0,
        })
        t["cases"] += 1
        if r.verdict in t:
            t[r.verdict] += 1
        if _is_manual(r):
            t["manual_cases"] += 1
            if r.verdict == "multi_better":
                t["manual_multi_better"] += 1
            elif r.verdict == "baseline_better":
                t["manual_baseline_better"] += 1
        elif r.verification == "verified_blind" and r.status == "completed":
            t["verified_cases"] += 1
            if r.verdict == "multi_better":
                t["verified_multi_better"] += 1
            elif r.verdict == "baseline_better":
                t["verified_baseline_better"] += 1
        else:
            t["unavailable_cases"] += 1
        sc = r.scores or {}
        t["multi_tokens"] += int(sc.get("multi_tokens") or 0)
        t["baseline_tokens"] += int(sc.get("baseline_tokens") or 0)
    for t in by_tier.values():
        verified_decided = t["verified_multi_better"] + t["verified_baseline_better"]
        manual_decided = t["manual_multi_better"] + t["manual_baseline_better"]
        t["win_rate"] = (t["verified_multi_better"] / verified_decided
                         if verified_decided else None)
        t["manual_win_rate"] = (t["manual_multi_better"] / manual_decided
                                if manual_decided else None)
        t["token_ratio"] = (round(t["multi_tokens"] / t["baseline_tokens"], 3)
                            if t["baseline_tokens"] else None)
    manual_total = sum(1 for r in rows if _is_manual(r))
    verified_total = sum(1 for r in rows if r.verification == "verified_blind"
                         and r.status == "completed")
    return {
        "suite_key": suite_key, "total": len(rows), "by_tier": by_tier,
        "verified_total": verified_total, "manual_total": manual_total,
        "unavailable_total": len(rows) - verified_total - manual_total,
        "disclaimer": "win_rate 仅统计已验证的双向盲评明确胜负；manual_win_rate "
                      "是手工粘贴产出的规则分，不代表盲评。不可用记录不进入胜率分母。",
    }


@router.get("/evals/{rid}")
async def get_eval(rid: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    run = await db.get(EvalRun, rid)
    if not run:
        raise HTTPException(404, "Eval run not found")
    await assert_company_capability(db, user.id, run.company_id, Capability.VIEW)
    return _row_dict(run)
