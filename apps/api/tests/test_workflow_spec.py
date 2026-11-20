"""workflow-spec v1：校验、编译、往返幂等、接口。

覆盖三类风险：
1. 校验器把明显错误放过（缺 END / 有环 / 变量没来源 / 员工不存在）；
2. 导入把不该落库的东西落库了（校验失败仍写入）；
3. 往返不幂等（导出→导入→导出字节不一致 → 进 git 全是假 diff）。
"""
import copy
import json

import pytest
import yaml
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.workflow import WorkflowDefinition, WorkflowNode
from app.spec import (SpecContext, build_json_schema, compile_spec, dump_spec,
                      errors, parse_spec, serialize_workflow, validate)
from app.spec.compiler import SpecParseError


def make_spec(**over) -> dict:
    spec = {
        "spec": "v1",
        "key": "prd-review",
        "name": "产品需求评审",
        "description": "评审 PRD 并给出结论",
        "inputs": [{"name": "prd", "type": "text", "required": True}],
        "nodes": [
            {"key": "start", "type": "START", "name": "开始", "x": 80, "y": 120},
            {"key": "analyze", "type": "AGENT", "name": "分析需求",
             "agent": "产品经理", "task": "分析以下 PRD：\n{{prd}}",
             "output": "requirements", "x": 310, "y": 120,
             "gate": {"acceptance": "列出核心需求与缺口",
                      "assert": {"min_chars": 300}, "on_fail": "observe"}},
            {"key": "end", "type": "END", "name": "结束", "x": 540, "y": 120},
        ],
        "edges": [{"from": "start", "to": "analyze"},
                  {"from": "analyze", "to": "end"}],
    }
    spec.update(over)
    return spec


def make_ctx() -> SpecContext:
    return SpecContext(agent_names={"产品经理": "agent-1"}, agent_ids={"agent-1"})


def canonical(spec: dict) -> dict:
    """导出是**规范化**的（节点按 key、连线按 from/to 排序），
    比较往返结果前先把输入也规范化，否则顺序差异会被误判成内容差异。"""
    s = copy.deepcopy(spec)
    s["nodes"] = sorted(s["nodes"], key=lambda n: n["key"])
    s["edges"] = sorted(s["edges"],
                        key=lambda e: (e["from"], e["to"], e.get("label") or ""))
    return s


def codes(issues) -> set[str]:
    return {i.code for i in errors(issues)}


# ---------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------

def test_valid_spec_has_no_errors():
    assert errors(validate(make_spec(), make_ctx())) == []


def test_missing_end_is_blocking():
    spec = make_spec(nodes=[
        {"key": "start", "type": "START", "name": "开始"},
        {"key": "a", "type": "AGENT", "name": "写", "agent": "产品经理",
         "task": "写点什么"},
    ], edges=[{"from": "start", "to": "a"}])
    issues = validate(spec, make_ctx())
    assert "missing_end" in codes(issues)
    assert "dead_end" in codes(issues)  # a 没有出边


def test_cycle_is_blocking():
    spec = make_spec(edges=[
        {"from": "start", "to": "analyze"},
        {"from": "analyze", "to": "end"},
        {"from": "end", "to": "analyze"},
    ])
    assert "cycle" in codes(validate(spec, make_ctx()))


def test_unknown_variable_is_blocking():
    spec = make_spec()
    spec["nodes"][1]["task"] = "分析 {{nope}}"
    issues = validate(spec, make_ctx())
    assert "unknown_var" in codes(issues)


def test_variable_from_downstream_is_blocking():
    """引用下游节点的产出别名 = 执行时还没有值，必须拦住。"""
    spec = make_spec()
    spec["nodes"].insert(2, {
        "key": "final", "type": "AGENT", "name": "定稿",
        "agent": "产品经理", "task": "定稿", "output": "draft",
    })
    spec["edges"] = [{"from": "start", "to": "analyze"},
                     {"from": "analyze", "to": "final"},
                     {"from": "final", "to": "end"}]
    spec["nodes"][1]["task"] = "分析 {{draft}}"   # 上游 analyze 引用了下游 final 的产出
    assert "var_not_upstream" in codes(validate(spec, make_ctx()))


def test_agent_must_exist_in_company():
    spec = make_spec()
    spec["nodes"][1]["agent"] = "查无此人"
    assert "agent_not_found" in codes(validate(spec, make_ctx()))


def test_unknown_gate_rule_is_blocking():
    spec = make_spec()
    spec["nodes"][1]["gate"] = {"assert": {"min_words": 300}}
    assert "unknown_assert" in codes(validate(spec, make_ctx()))


def test_unknown_top_field_typo_is_blocking():
    spec = make_spec()
    spec["nmae"] = "写错字段名"
    assert "unknown_field" in codes(validate(spec, make_ctx()))


def test_dangling_edge_is_blocking():
    spec = make_spec()
    spec["edges"].append({"from": "analyze", "to": "ghost"})
    assert "dangling_edge" in codes(validate(spec, make_ctx()))


def test_too_many_nodes_is_blocking():
    spec = make_spec()
    spec["nodes"] = [{"key": "start", "type": "START", "name": "开始"}]
    for i in range(25):
        spec["nodes"].append({"key": f"w{i}", "type": "WAIT", "name": f"等{i}",
                              "seconds": 1})
        spec["edges"].append({"from": "w%d" % (i - 1) if i else "start",
                              "to": f"w{i}"})
    spec["edges"].append({"from": "w24", "to": "end"})
    spec["nodes"].append({"key": "end", "type": "END", "name": "结束"})
    assert "too_many_nodes" in codes(validate(spec, make_ctx()))


def test_condition_without_labels_is_warning_only():
    spec = make_spec()
    spec["nodes"] = [
        {"key": "start", "type": "START", "name": "开始"},
        {"key": "branch", "type": "CONDITION", "name": "判断", "keyword": "通过"},
        {"key": "end", "type": "END", "name": "结束"},
    ]
    spec["edges"] = [{"from": "start", "to": "branch"},
                     {"from": "branch", "to": "end"}]
    issues = validate(spec, make_ctx())
    assert errors(issues) == []
    assert "condition_without_label" in {i.code for i in issues}


def test_parse_accepts_yaml_and_json_text():
    spec = make_spec()
    assert parse_spec(yaml.safe_dump(spec, allow_unicode=True))["key"] == "prd-review"
    assert parse_spec(json.dumps(spec))["key"] == "prd-review"
    assert parse_spec(spec) is spec


def test_parse_rejects_garbage():
    with pytest.raises(SpecParseError):
        parse_spec("just a sentence")
    with pytest.raises(SpecParseError):
        parse_spec("- a\n- b\n")


# ---------------------------------------------------------------------------
# 编译 + 往返
# ---------------------------------------------------------------------------

async def _seed_company_agent(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=co["id"], name="产品经理")
        db.add(agent)
        await db.commit()
        return user, co, agent


@pytest.mark.asyncio
async def test_compile_then_serialize_is_identical(client, make_user, make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    spec = make_spec()
    async with AsyncSessionLocal() as db:
        wf = await compile_spec(db, spec, company_id=co["id"], user_id=user["id"])
        await db.commit()
    async with AsyncSessionLocal() as db:
        out = await serialize_workflow(db, wf)
    assert out == canonical(spec)   # 往返：内容一致、顺序规范化


@pytest.mark.asyncio
async def test_round_trip_is_idempotent(client, make_user, make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    spec = make_spec()
    async with AsyncSessionLocal() as db:
        wf1 = await compile_spec(db, spec, company_id=co["id"], user_id=user["id"])
        await db.commit()
        out1 = await serialize_workflow(db, wf1)
        text1 = dump_spec(out1, "yaml")

    async with AsyncSessionLocal() as db:
        wf2 = await compile_spec(db, parse_spec(text1), company_id=co["id"],
                                 user_id=user["id"])
        await db.commit()
        out2 = await serialize_workflow(db, wf2)
        text2 = dump_spec(out2, "yaml")

    assert text1 == text2       # 第二个来回必须字节一致（进 git 不产生假 diff）


@pytest.mark.asyncio
async def test_interpolation_enabled_only_for_variable_nodes(client, make_user,
                                                             make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    spec = make_spec()
    spec["nodes"].insert(2, {
        "key": "review", "type": "AGENT", "name": "复核",
        "agent": "产品经理", "task": "复核结论，不要引用任何变量",
    })
    spec["edges"] = [{"from": "start", "to": "analyze"},
                     {"from": "analyze", "to": "review"},
                     {"from": "review", "to": "end"}]
    async with AsyncSessionLocal() as db:
        wf = await compile_spec(db, spec, company_id=co["id"], user_id=user["id"])
        await db.commit()
        q = await db.execute(select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id))
        nodes = {n.node_key: n for n in q.scalars()}

    assert nodes["analyze"].config.get("interpolate") is True
    assert nodes["analyze"].config["var_aliases"] == {}   # 输入变量不走别名
    assert not nodes["review"].config.get("interpolate")  # 没写变量 → 不开插值


@pytest.mark.asyncio
async def test_invalid_spec_writes_nothing(client, make_user, make_company):
    import sqlalchemy
    from app.spec.compiler import SpecInvalid

    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    bad = make_spec()
    bad["nodes"] = bad["nodes"][:2]          # 删掉 END
    async with AsyncSessionLocal() as db:
        with pytest.raises(SpecInvalid):
            await compile_spec(db, bad, company_id=co["id"], user_id=user["id"])
        await db.rollback()
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(sqlalchemy.func.count())
                              .select_from(WorkflowDefinition))).scalar()
    assert n == 0


async def test_subflow_key_resolves_and_exports_as_key(client, make_user, make_company):
    """子流程按 key 引用 → 编译成 id → 导出回到 key（spec 可跨公司复用）。"""
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    async with AsyncSessionLocal() as db:
        sub = WorkflowDefinition(company_id=co["id"], name="子流程",
                                 created_by=user["id"], meta={"spec_key": "sub-x"})
        db.add(sub)
        await db.commit()
        sub_id = sub.id

    spec = make_spec(nodes=[
        {"key": "start", "type": "START", "name": "开始"},
        {"key": "call", "type": "SUB_WORKFLOW", "name": "调用子流程",
         "workflow_key": "sub-x"},
        {"key": "end", "type": "END", "name": "结束"},
    ], edges=[{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    async with AsyncSessionLocal() as db:
        wf = await compile_spec(db, spec, company_id=co["id"], user_id=user["id"])
        await db.commit()
        out = await serialize_workflow(db, wf)
        q = await db.execute(select(WorkflowNode).where(
            WorkflowNode.workflow_id == wf.id,
            WorkflowNode.node_key == "call"))
        cfg = q.scalars().one().config

    assert cfg["workflow_id"] == sub_id                  # key 解析成了 id
    call = [n for n in out["nodes"] if n["key"] == "call"][0]
    assert call["workflow_key"] == "sub-x"               # 导出回到 key
    assert "workflow_id" not in call


def test_subflow_unknown_key_is_blocking():
    spec = make_spec(nodes=[
        {"key": "start", "type": "START", "name": "开始"},
        {"key": "call", "type": "SUB_WORKFLOW", "name": "调用", "workflow_key": "ghost"},
        {"key": "end", "type": "END", "name": "结束"},
    ], edges=[{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    ctx = SpecContext(workflow_keys={"sub-x": "id-1"}, workflow_ids={"id-1"})
    assert "subflow_not_found" in codes(validate(spec, ctx))


def test_subflow_requires_id_or_key():
    spec = make_spec(nodes=[
        {"key": "start", "type": "START", "name": "开始"},
        {"key": "call", "type": "SUB_WORKFLOW", "name": "调用"},
        {"key": "end", "type": "END", "name": "结束"},
    ], edges=[{"from": "start", "to": "call"}, {"from": "call", "to": "end"}])
    assert "missing_field" in codes(validate(spec, make_ctx()))


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_endpoint_reports_issues(client, make_user, make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    spec = make_spec()
    spec["nodes"][1]["agent"] = "查无此人"

    r = await client.post(f"/workflows/validate?company_id={co['id']}",
                          json={"spec": spec}, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert any(i["code"] == "agent_not_found" for i in body["issues"])
    assert any(i["path"].startswith("nodes[1]") for i in body["issues"])


@pytest.mark.asyncio
async def test_import_then_export_round_trip_via_http(client, make_user, make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    spec = make_spec()

    r = await client.post(f"/workflows/import?company_id={co['id']}",
                          json={"spec": dump_spec(spec, "yaml")},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    wid = r.json()["id"]

    r = await client.get(f"/workflows/{wid}/export?format=yaml",
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    exported = yaml.safe_load(r.text)
    assert canonical(exported) == canonical(spec)


@pytest.mark.asyncio
async def test_import_invalid_returns_422_and_writes_nothing(client, make_user,
                                                             make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    bad = make_spec()
    bad["nodes"] = bad["nodes"][:2]

    r = await client.post(f"/workflows/import?company_id={co['id']}",
                          json={"spec": bad}, headers=user["headers"])
    assert r.status_code == 422
    assert "missing_end" in r.text

    async with AsyncSessionLocal() as db:
        q = await db.execute(select(WorkflowDefinition))
        assert q.scalars().all() == []


@pytest.mark.asyncio
async def test_dry_run_validates_without_writing(client, make_user, make_company):
    user, co, agent = await _seed_company_agent(client, make_user, make_company)
    r = await client.post(f"/workflows/import?company_id={co['id']}",
                          json={"spec": make_spec(), "dry_run": True},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    async with AsyncSessionLocal() as db:
        q = await db.execute(select(WorkflowDefinition))
        assert q.scalars().all() == []


@pytest.mark.asyncio
async def test_schema_endpoint_is_public_and_wellformed(client):
    r = await client.get("/schemas/workflow.schema.json")   # 不带 Authorization
    assert r.status_code == 200
    schema = r.json()
    assert schema["properties"]["spec"]["const"] == "v1"
    assert "AGENT" in schema["properties"]["nodes"]["items"]["properties"]["type"]["enum"]


def test_json_schema_lists_every_node_type():
    from app.spec.schema import NODE_TYPES
    schema = build_json_schema()
    assert set(schema["properties"]["nodes"]["items"]["properties"]["type"]["enum"]) == set(NODE_TYPES)
