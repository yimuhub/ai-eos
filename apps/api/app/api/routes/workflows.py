"""Workflow CRUD + execution API (P2-02/P2-03 backend)."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, assert_company_owned,
                          get_current_user)
from app.core.permissions import Capability
from app.core.scope import build_ctx
from app.db.session import get_db
from app.models.workflow import (NodeType, RunStatus, WorkflowDefinition,
                                 WorkflowEdge, WorkflowExecution,
                                 WorkflowFeedback, WorkflowNode,
                                 WorkflowStep, WorkflowStatus)
from app.models.manual_log import MANUAL_REASONS, WorkflowManualLog
from app.models.project import Project
from app.models.workflow_gate import WorkflowStepGate
from app.schemas.responses import (WorkflowFeedbackItemOut,
                                   WorkflowFeedbackSubmitOut)
from app.services.event_system import log_event
from app.workflow_runtime import utcnow_iso
from app.report.renderer import render_execution_report
from app.gates.inherit import inherit_gates_for_nodes
from app.services.autonomy import compute_autonomy, merge_autonomy
from app.spec import (SpecParseError, build_context,
                      build_json_schema, compile_spec, dump_spec, errors,
                      parse_spec, serialize_workflow, summary, validate)

router = APIRouter(tags=["workflows"])


class NodeIn(BaseModel):
    node_key: str
    node_type: str
    name: str
    config: dict | None = {}
    position_x: float = 0
    position_y: float = 0


class EdgeIn(BaseModel):
    from_node: str
    to_node: str
    label: str | None = None


class WorkflowInputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", max_length=64)
    type: Literal["text", "number", "boolean", "json", "file"] = "text"
    required: StrictBool = False
    default: Any = None
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_default(self):
        if self.default is not None:
            _validate_input_value(self, self.default)
        return self


class WorkflowSaveIn(BaseModel):
    name: str
    description: str | None = None
    project_id: str | None = None
    inputs: list[WorkflowInputIn] = Field(default_factory=list, max_length=50)
    nodes: list[NodeIn]
    edges: list[EdgeIn]

    @model_validator(mode="after")
    def validate_unique_inputs(self):
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("输入名不能重复")
        return self


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


def _validate_input_value(item: WorkflowInputIn, value: Any) -> None:
    path = f"inputs.{item.name}"
    if item.type == "text":
        if not isinstance(value, str) or len(value) > 20_000:
            raise ValueError(f"{path} 必须是不超过 20000 字的字符串")
    elif item.type == "file":
        if not isinstance(value, str) or not value.strip() or len(value) > 1_000:
            raise ValueError(f"{path} 必须是不超过 1000 字的文件引用")
    elif item.type == "number":
        if type(value) not in (int, float) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError(f"{path} 必须是有限数字")
    elif item.type == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{path} 必须是布尔值")
    elif item.type == "json":
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} 必须是合法 JSON") from exc
        if len(encoded) > 64_000:
            raise ValueError(f"{path} 的 JSON 不能超过 64000 字符")


def _resolve_run_inputs(definitions: list[dict], supplied: dict) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise HTTPException(422, "inputs 必须是对象")
    declared = {item["name"]: item for item in definitions
                if isinstance(item, dict) and isinstance(item.get("name"), str)}
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise HTTPException(422, detail={"message": "存在未声明的运行入参",
                                         "fields": [f"inputs.{name}" for name in unknown]})
    resolved: dict[str, Any] = {}
    for name, item in declared.items():
        if name in supplied:
            value = supplied[name]
        elif "default" in item and item["default"] is not None:
            value = item["default"]
        elif item.get("required"):
            raise HTTPException(422, detail={"message": "缺少必填运行入参",
                                             "fields": [f"inputs.{name}"]})
        else:
            continue
        try:
            _validate_input_value(WorkflowInputIn.model_validate(item), value)
        except ValueError as exc:
            raise HTTPException(422, detail={"message": str(exc),
                                             "fields": [f"inputs.{name}"]}) from exc
        resolved[name] = value
    return resolved


_INPUT_VAR_RE = re.compile(r"\{\{\s*input\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _config_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _config_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _config_strings(child)


def _validate_input_references(nodes: list[NodeIn], definitions: list[dict]) -> None:
    names = {item.get("name") for item in definitions if isinstance(item, dict)}
    missing: set[str] = set()
    for node in nodes:
        for text in _config_strings(node.config or {}):
            missing.update(set(_INPUT_VAR_RE.findall(text)) - names)
    if missing:
        raise HTTPException(422, detail={
            "message": "节点引用了未声明的运行入参",
            "fields": [f"inputs.{name}" for name in sorted(missing)],
        })


def _validate_graph(data: WorkflowSaveIn) -> dict[str, str]:
    key_types: dict[str, str] = {}
    for n in data.nodes:
        try:
            key_types[n.node_key] = NodeType(n.node_type).value
        except ValueError:
            raise HTTPException(400, f"未知节点类型 {n.node_type}")
    if len(key_types) != len(data.nodes):
        raise HTTPException(400, "节点 key 不能重复")
    types = set(key_types.values())
    if NodeType.START.value not in types or NodeType.END.value not in types:
        raise HTTPException(400, "工作流必须包含开始和结束节点")

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for e in data.edges:
        if e.from_node not in key_types or e.to_node not in key_types:
            raise HTTPException(400, "连线端点必须是工作流内的有效节点")
        if e.from_node == e.to_node:
            raise HTTPException(400, "节点不能连接到自身")
        pair = (e.from_node, e.to_node)
        if pair in seen:
            raise HTTPException(400, "不能重复添加同一条连线")
        seen.add(pair)
        pairs.append(pair)
    if not pairs:
        raise HTTPException(400, "工作流没有任何连线")
    out_map: dict[str, list[str]] = {}
    for source, target in pairs:
        out_map.setdefault(source, []).append(target)
    no_out = sorted(k for k, t in key_types.items()
                    if t != NodeType.END.value and not out_map.get(k))
    if no_out:
        raise HTTPException(400, f"这些节点没有连到下一步：{', '.join(no_out[:5])}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise HTTPException(400, "工作流连线不能形成环")
        if key in visited:
            return
        visiting.add(key)
        for target in out_map.get(key, []):
            visit(target)
        visiting.remove(key)
        visited.add(key)

    for key in key_types:
        visit(key)
    reachable: set[str] = set()
    stack = [k for k, t in key_types.items() if t == NodeType.START.value]
    while stack:
        key = stack.pop()
        if key in reachable:
            continue
        reachable.add(key)
        stack.extend(out_map.get(key, []))
    if not reachable.intersection(k for k, t in key_types.items()
                                  if t == NodeType.END.value):
        raise HTTPException(400, "开始节点连不到结束节点，请检查连线方向")
    return key_types


def _execution_graph(ex: WorkflowExecution):
    snapshot = ex.definition_snapshot or {}
    if isinstance(snapshot, dict):
        return {
            n["node_key"]: WorkflowNode(
                workflow_id=ex.workflow_id,
                **{**n, "node_type": NodeType(n["node_type"])})
            for n in snapshot.get("nodes", [])
        }
    return {}


class SpecPayloadIn(BaseModel):
    """spec 接受 dict 或 YAML/JSON 文本（两种都允许，用户不必先猜格式）。

    `loadout_id` 非空时锁定阵容：AGENT 只能引用该阵容里的角色，防漏人/防幻觉。
    """

    spec: dict | str
    project_id: str | None = None
    dry_run: bool = False
    loadout_id: str | None = None


class ManualLogIn(BaseModel):
    node_key: str | None = None
    reason: str = "judgment"
    minutes: float = 0
    note: str | None = None


class ReworkIn(BaseModel):
    """步骤级返工请求。

    - `node_key` 省略 = 整轮重跑（从 START 起，全部步骤作废）；
    - `mode=rerun` = 让这个节点及其下游重做；
    - `mode=revise` = 把意见和上一版产出一起交回同一个员工，在原稿上改。
    """

    node_key: str | None = None
    mode: str = "rerun"
    feedback: str | None = None


class WorkflowFeedbackIn(BaseModel):
    success: bool | None = None
    score: float | None = None
    lessons: list[str] = []
    note: str | None = None
    optimize: bool = True


def _workflow_scope(ctx):
    """工作流行的可见条件：owner/admin 看全公司，其余只看自己创建的。

    WorkflowDefinition 没有部门字段，只能按创建者收窄——
    宁可让成员少看到一些，也不把别人的编排与执行历史摊开。
    """
    base = WorkflowDefinition.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    return and_(base, WorkflowDefinition.created_by == ctx.user_id)


async def _wf_owned(db: AsyncSession, user, wf_id: str,
                    capability: Capability = Capability.VIEW) -> WorkflowDefinition:
    wf = await db.get(WorkflowDefinition, wf_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await assert_company_capability(db, user.id, wf.company_id, capability)
    ctx = await build_ctx(db, user, wf.company_id)
    found = (await db.execute(
        select(WorkflowDefinition.id).where(WorkflowDefinition.id == wf.id,
                                            _workflow_scope(ctx))
        .limit(1))).first()
    if found is None:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.get("/workflows")
async def list_workflows(company_id: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    ctx = await build_ctx(db, user, company_id)
    q = await db.execute(select(WorkflowDefinition).where(_workflow_scope(ctx)))
    return [{"id": w.id, "name": w.name, "description": w.description,
             "status": w.status.value, "version": w.version,
             "source": w.source, "score": w.score, "run_count": w.run_count,
             "success_count": w.success_count,
             "failure_count": w.failure_count,
             "reuse_key": w.reuse_key or "",
             "previous_workflow_id": w.previous_workflow_id or ""}
            for w in q.scalars()]


@router.get("/workflows/{wid}")
async def get_workflow(wid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid)
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wid))
    edges_q = await db.execute(select(WorkflowEdge).where(
        WorkflowEdge.workflow_id == wid))
    feedback_q = await db.execute(
        select(WorkflowFeedback).where(WorkflowFeedback.workflow_id == wid)
        .order_by(WorkflowFeedback.created_at.desc()).limit(10))
    return {
        "id": wf.id, "name": wf.name, "description": wf.description,
        "status": wf.status.value, "version": wf.version,
        "source": wf.source, "score": wf.score, "run_count": wf.run_count,
        "success_count": wf.success_count, "failure_count": wf.failure_count,
        "reuse_key": wf.reuse_key or "",
        "previous_workflow_id": wf.previous_workflow_id or "",
        "owner_agent_id": wf.owner_agent_id or "",
        "inputs": (wf.meta or {}).get("inputs") or [],
        "meta": wf.meta or {},
        "nodes": [{c.key: getattr(n, c.key) for c in n.__table__.columns}
                  for n in nodes_q.scalars()],
        "edges": [{"from_node": e.from_node, "to_node": e.to_node, "label": e.label}
                  for e in edges_q.scalars()],
        "feedback": [_feedback_out(f) for f in feedback_q.scalars()],
    }


def _feedback_out(row: WorkflowFeedback) -> dict:
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "success": bool(row.success),
        "score": float(row.score or 0),
        "metrics": row.metrics or {},
        "lessons": row.lessons or [],
        "note": row.note or "",
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


@router.get("/workflows/{wid}/feedback",
            response_model=list[WorkflowFeedbackItemOut])
async def list_workflow_feedback(wid: str,
                                 limit: int = Query(20, ge=1, le=100),
                                 db: AsyncSession = Depends(get_db),
                                 user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid)
    rows = (await db.execute(
        select(WorkflowFeedback).where(WorkflowFeedback.workflow_id == wf.id)
        .order_by(WorkflowFeedback.created_at.desc()).limit(limit)
    )).scalars().all()
    return [_feedback_out(f) for f in rows]


@router.post("/workflows/{wid}/feedback",
             response_model=WorkflowFeedbackSubmitOut)
async def submit_workflow_feedback(wid: str, data: WorkflowFeedbackIn,
                                   db: AsyncSession = Depends(get_db),
                                   user=Depends(get_current_user)):
    """人工反馈一次运行结果：写入评分与经验，必要时立即升一版。

    自动反馈由 CEO 收尾时写入；这个接口给「手动跑工作流」和老板复盘用。
    """
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
    if data.score is None and data.success is None:
        raise HTTPException(400, "success 与 score 至少给一个")
    score = data.score if data.score is not None else (
        100.0 if data.success else 0.0)
    score = max(0.0, min(float(score), 100.0))
    success = data.success if data.success is not None else score >= 60

    from app.workflow_authoring import (OPTIMIZE_THRESHOLD, collect_lessons,
                                        optimize_workflow, record_feedback,
                                        workflow_brief)
    row = await record_feedback(
        db, wf, success=success, score=score, lessons=data.lessons,
        note=data.note, source="manual", created_by=user.id)
    target = wf
    optimized_from = ""
    if data.optimize and score < OPTIMIZE_THRESHOLD:
        lessons = await collect_lessons(db, wf)
        new = await optimize_workflow(db, wf, lessons)
        if new is not None:
            target = new
            optimized_from = wf.id
    await db.commit()
    await db.refresh(row)
    await db.refresh(target)
    return {"feedback": _feedback_out(row), "workflow": workflow_brief(target),
            "optimized_from": optimized_from}


async def start_execution(db: AsyncSession, workflow_id: str,
                          project_id: str | None, started_by: str,
                          input_data: dict | None = None) -> WorkflowExecution:
    from app.workflow_runtime import advance_execution, start_workflow
    ex = await start_workflow(db, workflow_id, started_by, project_id, input_data)
    return ex


@router.post("/workflows/validate")
async def validate_workflow_spec(data: SpecPayloadIn, company_id: str,
                                 db: AsyncSession = Depends(get_db),
                                 user=Depends(get_current_user)):
    """只校验、不写库。返回问题清单，前端可直接逐条高亮到 nodes[i]。"""
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)
    try:
        spec = parse_spec(data.spec)
    except SpecParseError as e:
        raise HTTPException(400, str(e))
    ctx = await build_context(db, company_id)
    issues = validate(spec, ctx)
    return {"ok": not errors(issues),
            "issues": [i.to_dict() for i in issues],
            "summary": summary(issues)}


@router.post("/workflows/import")
async def import_workflow_spec(data: SpecPayloadIn, company_id: str,
                               db: AsyncSession = Depends(get_db),
                               user=Depends(get_current_user)):
    """校验通过才落库；dry_run 时等价于 validate 但不写。

    注意：导入是**显式动作**。导出的文件永远不会自动回写数据库——
    否则"文件改了但库里没改"的分裂迟早发生。
    """
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)
    try:
        spec = parse_spec(data.spec)
    except SpecParseError as e:
        raise HTTPException(400, str(e))

    ctx = await build_context(db, company_id)
    issues = validate(spec, ctx)
    if data.loadout_id:
        issues = issues + await _loadout_lock_issues(
            db, company_id, data.loadout_id, spec)
    if data.dry_run or errors(issues):
        body = {"ok": not errors(issues),
                "issues": [i.to_dict() for i in issues],
                "summary": summary(issues)}
        if errors(issues):
            raise HTTPException(422, detail=body)
        return body

    wf = await compile_spec(db, spec, company_id=company_id, user_id=user.id,
                            project_id=data.project_id, ctx=ctx)
    # 角色档案的出厂验收标准：只给没写 gate 的 AGENT 节点补，且强制观察态
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wf.id))
    inherited = await inherit_gates_for_nodes(db, list(nodes_q.scalars()))
    await log_event(db, "workflow.imported", "workflow_definition", wf.id,
                    "user", user.id,
                    {"key": (wf.meta or {}).get("spec_key"),
                     "nodes": len(spec.get("nodes") or []),
                     "gates_inherited": inherited})
    await db.commit()
    return {"id": wf.id, "name": wf.name,
            "key": (wf.meta or {}).get("spec_key"),
            "nodes": len(spec.get("nodes") or []),
            "edges": len(spec.get("edges") or []),
            "gates_inherited": inherited}


async def _loadout_lock_issues(db: AsyncSession, company_id: str,
                               loadout_id: str, spec: dict) -> list:
    """阵容锁定：spec 里的 AGENT 必须是该 loadout 里的角色。"""
    from app.api.routes.teams import locked_role_names
    from app.spec.compiler import _resolve_agent
    from app.spec.validator import Issue, _norm_name

    allowed = await locked_role_names(db, company_id, loadout_id)
    ctx = await build_context(db, company_id)
    out = []
    for i, n in enumerate(spec.get("nodes") or []):
        if not isinstance(n, dict) or n.get("type") != "AGENT":
            continue
        aid = _resolve_agent(n, ctx)
        canonical = ctx.agent_names_by_id.get(aid) if aid else None
        name = n.get("agent")
        if (isinstance(name, str) and name and canonical
                and _norm_name(name) != _norm_name(canonical)):
            out.append(Issue("agent_identity_mismatch",
                             "agent 与 agent_id 指向不同的员工",
                             f"nodes[{i}].agent"))
            continue
        display = canonical or name or aid or "未指定员工"
        if canonical and _norm_name(canonical) not in allowed:
            out.append(Issue("agent_not_in_loadout",
                             f"员工「{display}」不在该阵容（loadout）内",
                             f"nodes[{i}].agent"))
    return out


@router.get("/workflows/{wid}/export")
async def export_workflow_spec(wid: str, format: str = "yaml",
                               db: AsyncSession = Depends(get_db),
                               user=Depends(get_current_user)):
    """把库里的一张工作流还原成 spec。只读，不落文件、不回写。"""
    if format not in ("yaml", "json"):
        raise HTTPException(400, "format 只能是 yaml 或 json")
    wf = await _wf_owned(db, user, wid, Capability.VIEW)
    spec = await serialize_workflow(db, wf)
    text = dump_spec(spec, format)
    media = "application/json" if format == "json" else "application/yaml"
    filename = f"{(wf.meta or {}).get('spec_key') or wid}.{format}"
    return Response(content=text, media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/schemas/workflow.schema.json")
async def workflow_spec_schema():
    """给编辑器用的 JSON Schema（无需登录：静态内容，不含任何租户数据）。

    文件首行写 `# yaml-language-server: $schema=<本服务>/schemas/workflow.schema.json`
    即可在 VS Code / JetBrains 里获得字段补全与拼写标红。
    """
    from fastapi.responses import JSONResponse
    return JSONResponse(build_json_schema(),
                        headers={"Cache-Control": "public, max-age=3600"})


async def _validate_agent_tenant(db: AsyncSession, company_id: str,
                                data: WorkflowSaveIn) -> None:
    from app.models.agent import Agent
    for node in data.nodes:
        if NodeType(node.node_type) != NodeType.AGENT:
            continue
        agent_id = (node.config or {}).get("agent_id")
        agent = await db.get(Agent, agent_id) if agent_id else None
        if agent is None or agent.company_id != company_id:
            raise HTTPException(400, f"节点 {node.node_key} 的执行员工不存在或不属于本公司")


async def _validate_workflow_relations(
    db: AsyncSession, user, company_id: str, data: WorkflowSaveIn
) -> None:
    if data.project_id:
        project = await db.get(Project, data.project_id)
        if not project or project.company_id != company_id:
            raise HTTPException(404, "项目不存在或不属于本公司")
        await assert_company_owned(db, user.id, company_id)
    await _validate_agent_tenant(db, company_id, data)
    for node in data.nodes:
        if NodeType(node.node_type) != NodeType.SUB_WORKFLOW:
            continue
        child_id = (node.config or {}).get("workflow_id")
        if not child_id:
            raise HTTPException(400, f"节点 {node.node_key} 必须指定 workflow_id")
        child = await _wf_owned(db, user, str(child_id))
        if child.company_id != company_id:
            raise HTTPException(400, f"节点 {node.node_key} 的子工作流不属于本公司")


@router.post("/workflows", status_code=201)
async def save_workflow(data: WorkflowSaveIn, company_id: str,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    # 保存/覆盖编排是日常写操作（OPERATE）
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)

    _validate_graph(data)
    _validate_input_references(data.nodes, [item.model_dump() for item in data.inputs])
    await _validate_workflow_relations(db, user, company_id, data)

    inputs = [item.model_dump(exclude_none=True) for item in data.inputs]
    wf = WorkflowDefinition(company_id=company_id, project_id=data.project_id,
                            name=data.name, description=data.description,
                            status=WorkflowStatus.PUBLISHED, created_by=user.id,
                            meta={"inputs": inputs})
    db.add(wf)
    await db.flush()
    for n in data.nodes:
        try:
            nt = NodeType(n.node_type)
        except ValueError:
            raise HTTPException(400, f"未知节点类型 {n.node_type}")
        db.add(WorkflowNode(workflow_id=wf.id, node_key=n.node_key,
                            node_type=nt, name=n.name,
                            config=n.config or {},
                            position_x=n.position_x, position_y=n.position_y))
    for e in data.edges:
        db.add(WorkflowEdge(workflow_id=wf.id, from_node=e.from_node,
                            to_node=e.to_node, label=e.label))
    await log_event(db, "workflow.saved", "workflow_definition", None, "user",
                    user.id, {"name": data.name, "nodes": len(data.nodes)})
    await db.commit()
    return {"id": wf.id, "name": wf.name}


@router.put("/workflows/{wid}")
async def update_workflow(wid: str, data: WorkflowSaveIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """更新已有工作流（修复前只有 POST：编辑保存会新建重复定义）。"""
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
    _validate_graph(data)
    _validate_input_references(data.nodes, [item.model_dump() for item in data.inputs])
    await _validate_workflow_relations(db, user, wf.company_id, data)
    inputs = [item.model_dump(exclude_none=True) for item in data.inputs]
    active_statuses = [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED,
                       RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT]
    active = await db.execute(select(WorkflowExecution.id).where(
        WorkflowExecution.workflow_id == wid,
        WorkflowExecution.status.in_(active_statuses)).limit(1))
    if active.first():
        raise HTTPException(409, "工作流仍有未结束的执行实例，暂不能修改定义")

    expected_version = wf.version or 1
    claimed = await db.execute(update(WorkflowDefinition).where(
        WorkflowDefinition.id == wid,
        WorkflowDefinition.version == expected_version,
        ~select(WorkflowExecution.id).where(
            WorkflowExecution.workflow_id == wid,
            WorkflowExecution.status.in_(active_statuses)).exists()
    ).values(version=expected_version + 1).execution_options(
        synchronize_session=False))
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "工作流已变化或有新执行启动，请刷新后重试")
    wf.name = data.name
    wf.description = data.description
    wf.project_id = data.project_id
    wf.meta = {**(wf.meta or {}), "inputs": inputs}
    wf.version = expected_version + 1
    await db.execute(delete(WorkflowNode).where(WorkflowNode.workflow_id == wid))
    await db.execute(delete(WorkflowEdge).where(WorkflowEdge.workflow_id == wid))
    for n in data.nodes:
        nt = NodeType(n.node_type)
        db.add(WorkflowNode(workflow_id=wid, node_key=n.node_key,
                            node_type=nt, name=n.name,
                            config=n.config or {},
                            position_x=n.position_x, position_y=n.position_y))
    for e in data.edges:
        db.add(WorkflowEdge(workflow_id=wid, from_node=e.from_node,
                            to_node=e.to_node, label=e.label))
    await log_event(db, "workflow.updated", "workflow_definition", wid, "user",
                    user.id, {"name": data.name, "version": wf.version})
    await db.commit()
    return {"id": wid, "name": wf.name, "version": wf.version}


@router.delete("/workflows/{wid}", status_code=204)
async def delete_workflow(wid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    # 删工作流不可逆（节点/连线一并消失），按 DESTRUCTIVE 处理
    wf = await _wf_owned(db, user, wid, Capability.DESTRUCTIVE)
    active_statuses = [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED,
                       RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT]
    active = await db.execute(select(WorkflowExecution.id).where(
        WorkflowExecution.workflow_id == wid,
        WorkflowExecution.status.in_(active_statuses)).limit(1))
    if active.first():
        raise HTTPException(409, "工作流仍有未结束的执行实例，暂不能删除定义")
    await db.delete(wf)
    await db.commit()


# ---------- Execution ----------
def _run_in_background(execution_id: str):
    import app.background_jobs as background_jobs

    async def _job(session):
        from app.workflow_runtime import advance_execution
        await advance_execution(session, execution_id)

    background_jobs.submit_async(_job)


@router.post("/workflows/{wid}/run", status_code=202)
async def run_workflow(wid: str, background: BackgroundTasks,
                       data: RunIn | None = None,
                       project_id: str | None = Query(None),
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    wf = await _wf_owned(db, user, wid, Capability.OPERATE)
    if wf.status == WorkflowStatus.ARCHIVED:
        raise HTTPException(400, "工作流已归档，不能执行")
    body = data or RunIn()
    if project_id and body.project_id and project_id != body.project_id:
        raise HTTPException(422, "query 与 body 的 project_id 不一致")
    target_project = body.project_id or project_id or wf.project_id
    if target_project:
        project = await db.get(Project, target_project)
        if not project or project.company_id != wf.company_id:
            raise HTTPException(404, "项目不存在或不属于本公司")
        await assert_company_owned(db, user.id, wf.company_id)
    definitions = (wf.meta or {}).get("inputs") or []
    if not isinstance(definitions, list):
        raise HTTPException(409, "工作流输入声明损坏，请重新保存定义")
    resolved = _resolve_run_inputs(definitions, body.inputs)
    nodes_q = await db.execute(select(WorkflowNode).where(
        WorkflowNode.workflow_id == wid))
    nodes = [NodeIn.model_validate({
        "node_key": n.node_key, "node_type": n.node_type.value, "name": n.name,
        "config": n.config or {}, "position_x": n.position_x,
        "position_y": n.position_y,
    }) for n in nodes_q.scalars()]
    _validate_input_references(nodes, definitions)
    ex = await start_execution(db, wid, target_project, user.id, resolved)
    execution_id = ex.id
    _run_in_background(execution_id)
    return {"detail": "started", "execution_id": execution_id,
            "inputs": resolved}


@router.get("/executions/{eid}")
async def get_execution(eid: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await _wf_owned(db, user, ex.workflow_id)

    # 重启/丢失调度的惰性恢复：状态还在跑但没有任何 worker 持有租约时，
    # 看板轮询到这里就把实例重新踢起来（租约保证不会和在跑的 worker 双跑）。
    from app.workflow_runtime import execution_is_orphaned
    if await execution_is_orphaned(db, ex):
        await log_event(db, "workflow.recovered", "workflow_execution", eid,
                        "system", None, {"status": ex.status.value})
        await db.commit()
        _run_in_background(eid)

    steps_q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == eid).order_by(WorkflowStep.created_at))
    graph = _execution_graph(ex)
    node_names = {n.node_key: n.name for n in graph.values()}
    if not graph:
        nodes_q = await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == ex.workflow_id))
        node_names = {n.node_key: n.name for n in nodes_q.scalars()}

    steps = []
    for s in steps_q.scalars():
        d = {c.key: getattr(s, c.key) for c in s.__table__.columns}
        d["node_type"] = s.node_type.value
        d["status"] = s.status.value
        d["node_name"] = node_names.get(s.node_key, s.node_key)
        steps.append(d)

    # 验收闸门证据：每个节点每轮一条，前端据此显示"哪条规则没过、实际值多少"。
    # 只读附加字段，不影响既有响应结构。
    gates_q = await db.execute(select(WorkflowStepGate).where(
        WorkflowStepGate.execution_id == eid).order_by(
        WorkflowStepGate.created_at, WorkflowStepGate.gate_round))
    gates = []
    for g in gates_q.scalars():
        gates.append({
            "id": g.id, "node_key": g.node_key, "node_name": node_names.get(g.node_key, g.node_key),
            "attempt": g.attempt, "gate_round": g.gate_round, "stage": g.stage,
            "checks": g.checks or [], "passed": g.passed, "blocking": g.blocking,
            "degraded": g.degraded, "score": g.score,
            "judge_model": g.judge_model, "tokens": g.tokens,
            "latency_ms": g.latency_ms, "detail": g.detail,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        })

    return {"id": ex.id, "workflow_id": ex.workflow_id, "workflow_name": wf.name,
            "project_id": ex.project_id, "status": ex.status.value,
            "current_nodes": ex.current_nodes, "error": ex.error,
            "steps": steps, "gates": gates}


@router.post("/executions/{eid}/pause")
async def pause_execution(eid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    ex = await _owned_execution(db, user, eid)
    # CAS：只有仍在"可暂停"状态的那一次生效。推进循环每步前重读状态，
    # 所以暂停能立刻打断后续节点。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.in_([
                   RunStatus.RUNNING, RunStatus.PENDING,
                   RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL]))
        .values(status=RunStatus.PAUSED)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot pause from {ex.status.value}")
    return {"detail": "paused"}


@router.post("/executions/{eid}/resume")
async def resume_execution(eid: str, background: BackgroundTasks,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user, eid)
    # CAS PAUSED→PENDING：并发点两次「继续」只会有一次提交后台推进，
    # 也避免第二次把正在 RUNNING 的实例再拉一份 worker。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status == RunStatus.PAUSED)
        .values(status=RunStatus.PENDING)
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot resume from {ex.status.value}")
    _run_in_background(eid)
    return {"detail": "resumed"}


@router.post("/executions/{eid}/cancel")
async def cancel_execution(eid: str, reason: str | None = None,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    ex = await _owned_execution(db, user, eid)
    # CAS：取消对"还没结束"的实例都生效（含等人工/暂停/排队），
    # 推进循环每步前重读状态，读到 CANCELLED 立刻停手。
    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.notin_([RunStatus.COMPLETED,
                                                RunStatus.CANCELLED]))
        .values(status=RunStatus.CANCELLED,
                error=f"cancelled by user: {reason or 'no reason'}")
        .execution_options(synchronize_session=False))
    await db.commit()
    if res.rowcount != 1:
        await db.refresh(ex)
        raise HTTPException(409, f"cannot cancel from {ex.status.value}")
    await log_event(db, "workflow.cancelled", "workflow_execution", eid,
                    "user", user.id, {"reason": reason})
    await db.commit()
    return {"detail": "cancelled"}


@router.post("/executions/{eid}/approve/{node_key}")
async def approve_node(eid: str, node_key: str, approve: bool = True,
                       human_input: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """人工审批/输入后继续执行。"""
    ex = await _owned_execution(db, user, eid)

    node = _execution_graph(ex).get(node_key)
    if not node:
        raise HTTPException(404, "node not found")
    if node_key not in (ex.current_nodes or []):
        raise HTTPException(409, "该节点不是当前等待人工处理的节点")
    expected_status = (RunStatus.WAITING_APPROVAL
                       if node.node_type == NodeType.APPROVAL
                       else RunStatus.WAITING_INPUT)
    if node.node_type not in (NodeType.APPROVAL, NodeType.HUMAN_INPUT):
        raise HTTPException(400, "该节点不需要人工审批或输入")
    if ex.status != expected_status:
        raise HTTPException(409, f"execution is {ex.status.value}, not waiting")
    if node.node_type == NodeType.HUMAN_INPUT and human_input is None:
        raise HTTPException(400, "人工输入节点必须提供 human_input")

    steps_q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == eid,
        WorkflowStep.node_key == node_key,
        WorkflowStep.status == RunStatus.RUNNING
    ).order_by(WorkflowStep.attempt.desc()).limit(1))
    step = steps_q.scalars().first()
    if step is None:
        raise HTTPException(409, "该节点没有可审批的打开步骤")
    step.input_data = {
        **(step.input_data or {}),
        "_human_decision": {
            "approved": bool(approve), "human_input": human_input,
            "decided_by": user.id, "decided_at": utcnow_iso(),
        },
    }

    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status == expected_status)
        .values(status=RunStatus.CANCELLED
                if not approve and node.node_type == NodeType.APPROVAL
                else RunStatus.PENDING,
                error=("rejected at approval node"
                       if not approve and node.node_type == NodeType.APPROVAL
                       else None))
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        await db.rollback()
        await db.refresh(ex)
        raise HTTPException(409, f"execution is {ex.status.value}, not waiting")
    await db.commit()

    if approve or node.node_type == NodeType.HUMAN_INPUT:
        _run_in_background(eid)
        return {"detail": "approved, continuing"}
    # 拒绝审批 → 取消执行（状态已在上面 CAS 里落成 CANCELLED）
    return {"detail": "rejected, execution cancelled"}


@router.post("/executions/{eid}/rework", status_code=202)
async def rework_execution(eid: str, data: ReworkIn, background: BackgroundTasks,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """步骤级返工：整轮 / 从某步重跑 / 带意见在原稿上改。

    上游步骤一行不重跑（token 为 0）；下游是否重跑由各自的输入指纹决定，
    所以菱形依赖里改 A，D 只会跑一次。
    """
    ex = await _owned_execution(db, user, eid)
    if data.mode not in ("rerun", "revise"):
        raise HTTPException(400, "mode 只能是 rerun 或 revise")

    nodes = _execution_graph(ex)
    if not nodes:
        nodes_q = await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == ex.workflow_id))
        nodes = {n.node_key: n for n in nodes_q.scalars()}
    if not nodes:
        raise HTTPException(400, "工作流没有节点")

    if not data.node_key:
        if data.mode == "revise":
            raise HTTPException(400, "带意见返工必须指定 node_key")
        start = next((n for n in nodes.values()
                      if n.node_type == NodeType.START), None)
        if not start:
            raise HTTPException(400, "工作流没有 START 节点")
        target = start.node_key
        full_rerun = True
    else:
        node = nodes.get(data.node_key)
        if not node:
            raise HTTPException(404, f"节点 {data.node_key} 不存在")
        if node.node_type != NodeType.AGENT:
            raise HTTPException(400, "步骤级返工只支持已成功执行的 AGENT 节点")
        completed = await db.execute(select(WorkflowStep.id).where(
            WorkflowStep.execution_id == eid,
            WorkflowStep.node_key == node.node_key,
            WorkflowStep.status == RunStatus.COMPLETED).limit(1))
        if completed.first() is None:
            raise HTTPException(409, "不能返工尚未成功执行的节点")
        target = node.node_key
        full_rerun = False
    if data.mode == "revise" and not (data.feedback or "").strip():
        raise HTTPException(400, "revise 模式必须给 feedback")

    # 只有"停了"的实例才能返工：跑着的实例返工会让两拨 worker 抢同一批节点
    allowed = [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED]
    if ex.status not in allowed:
        raise HTTPException(409, f"cannot rework from {ex.status.value}")

    res = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == eid,
               WorkflowExecution.status.in_(allowed))
        .values(status=RunStatus.RUNNING, error=None,
                current_nodes=[target])
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise HTTPException(409, "execution status changed, try again")
    await db.refresh(ex)

    # 作废范围：整轮 = 全部；单步 = 该节点所有历史完成记录（不止最新一条，
    # 否则可能被"指纹复用"命中更早的版本）。
    stale_q = update(WorkflowStep).where(
        WorkflowStep.execution_id == eid,
        WorkflowStep.status == RunStatus.COMPLETED,
        WorkflowStep.stale_at.is_(None))
    if not full_rerun:
        stale_q = stale_q.where(WorkflowStep.node_key == target)
    await db.execute(stale_q.values(stale_at=utcnow_iso())
                     .execution_options(synchronize_session=False))

    rework = {"node_key": target, "mode": data.mode,
              "feedback": (data.feedback or "").strip() or None,
              "at": utcnow_iso()}
    ex.input_data = {**(ex.input_data or {}), "_rework": rework}
    await log_event(db, "workflow.reworked", "workflow_execution", eid,
                    "user", user.id, {"node": target, "mode": data.mode,
                                      "full": full_rerun})
    await db.commit()
    _run_in_background(eid)
    return {"detail": "rework started", "node_key": target,
            "mode": data.mode, "full_rerun": full_rerun}


async def _execution_steps(db: AsyncSession, eid: str) -> tuple[list[dict], dict]:
    q = await db.execute(select(WorkflowStep).where(
        WorkflowStep.execution_id == eid).order_by(WorkflowStep.created_at))
    steps = []
    for s in q.scalars():
        steps.append({
            "node_key": s.node_key, "node_type": s.node_type.value,
            "status": s.status.value, "attempt": s.attempt,
            "input_data": s.input_data, "output_data": s.output_data,
            "error": s.error, "started_at": s.started_at,
            "completed_at": s.completed_at, "stale_at": s.stale_at,
        })
    return steps, {}


async def _execution_gates(db: AsyncSession, eid: str,
                           node_names: dict) -> list[dict]:
    q = await db.execute(select(WorkflowStepGate).where(
        WorkflowStepGate.execution_id == eid).order_by(
        WorkflowStepGate.created_at, WorkflowStepGate.gate_round))
    out = []
    for g in q.scalars():
        out.append({
            "node_key": g.node_key,
            "node_name": node_names.get(g.node_key, g.node_key),
            "attempt": g.attempt, "gate_round": g.gate_round,
            "stage": g.stage, "checks": g.checks or [],
            "passed": g.passed, "degraded": g.degraded,
            "score": g.score, "tokens": g.tokens, "latency_ms": g.latency_ms,
            "detail": g.detail,
        })
    return out


async def _manual_logs(db: AsyncSession, eids: list[str]) -> list[dict]:
    if not eids:
        return []
    q = await db.execute(select(WorkflowManualLog).where(
        WorkflowManualLog.execution_id.in_(eids)).order_by(
        WorkflowManualLog.created_at))
    return [{"node_key": r.node_key, "reason": r.reason,
             "minutes": r.minutes, "note": r.note} for r in q.scalars()]


@router.post("/executions/{eid}/manual-log", status_code=201)
async def add_manual_log(eid: str, data: ManualLogIn,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """记一次人工介入（自主率的"人工侧"证据）。"""
    await _owned_execution(db, user, eid)
    if data.reason not in MANUAL_REASONS:
        raise HTTPException(400, f"reason 只能是 {'/'.join(MANUAL_REASONS)}")
    if data.minutes < 0 or data.minutes > 1440:
        raise HTTPException(400, "minutes 必须在 0~1440 之间")
    log = WorkflowManualLog(execution_id=eid, node_key=data.node_key,
                            reason=data.reason, minutes=data.minutes,
                            note=data.note, created_by=user.id)
    db.add(log)
    await db.commit()
    return {"id": log.id, "execution_id": eid, "reason": log.reason,
            "minutes": log.minutes, "note": log.note}


@router.get("/executions/{eid}/autonomy")
async def execution_autonomy(eid: str, db: AsyncSession = Depends(get_db),
                             user=Depends(get_current_user)):
    await _owned_execution(db, user, eid)
    steps, _ = await _execution_steps(db, eid)
    logs = await _manual_logs(db, [eid])
    return {"execution_id": eid, **compute_autonomy(steps, logs)}


@router.get("/executions/{eid}/report.html")
async def execution_report(eid: str, db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """单文件 HTML 报告：全内联样式、零外链，断网可开。"""
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await _wf_owned(db, user, ex.workflow_id)
    graph = _execution_graph(ex)
    node_names = {n.node_key: n.name for n in graph.values()}
    if not graph:
        nodes_q = await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == ex.workflow_id))
        node_names = {n.node_key: n.name for n in nodes_q.scalars()}
    steps, _ = await _execution_steps(db, eid)
    for s in steps:
        s["node_name"] = node_names.get(s["node_key"], s["node_key"])
    gates = await _execution_gates(db, eid, node_names)
    logs = await _manual_logs(db, [eid])
    html_doc = render_execution_report(
        workflow_name=wf.name,
        execution={"id": ex.id, "status": ex.status.value, "error": ex.error},
        steps=steps, gates=gates,
        autonomy=compute_autonomy(steps, logs))
    return Response(content=html_doc, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition":
                             f'inline; filename="report-{eid[:8]}.html"'})


@router.get("/metrics/autonomy")
async def company_autonomy(company_id: str, days: int = 30,
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    """公司口径自主率：分母相加，不对各执行的比率求平均。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    q = await db.execute(
        select(WorkflowExecution.id).join(
            WorkflowDefinition,
            WorkflowExecution.workflow_id == WorkflowDefinition.id)
        .where(WorkflowDefinition.company_id == company_id,
               WorkflowExecution.created_at >= since))
    eids = [r[0] for r in q.all()]
    parts = []
    for eid in eids:
        steps, _ = await _execution_steps(db, eid)
        parts.append(compute_autonomy(steps, []))
    logs = await _manual_logs(db, eids)
    return {"company_id": company_id, "days": days,
            **merge_autonomy(parts, logs)}


async def _owned_execution(db: AsyncSession, user, eid: str) -> WorkflowExecution:
    ex = await db.get(WorkflowExecution, eid)
    if not ex:
        raise HTTPException(404, "Execution not found")
    # 跑/停/继续/审批都是对编排的写操作
    await _wf_owned(db, user, ex.workflow_id, Capability.OPERATE)
    return ex
