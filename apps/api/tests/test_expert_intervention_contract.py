"""干预审批门（``/intervention``）与专家库（``/experts``）的响应形状契约（A3 长尾）。

和 `test_kb_contract.py` 同一套路：把「端点真实返回的键集合」和「`response_model`
声明的字段」两头钉死，两端都用**相等**而不是"包含"——只查一边会漏：

* 只断言响应里有某些键 → 端点哪天多吐一个键测试照样绿，前端永远不知道；
* 只断言模型字段 → 模型写漏一个字段时测试不会红，但序列化会**静默把它裁掉**，
  于是审批中心的「始终允许」勾了没反应、专家卡片少一列，且没有任何报错。

这两个模块的特殊性在于**分支返回特别多**，所以每个分支的键集都单独钉：

* `GET /intervention/policy`：库里有策略（带 `id`、不带 `is_default`）↔ 没策略回默认
  （反之）。用 `response_model_exclude_unset=True` 才能让两条分支各回各的键，
  这里两头都断言，防止有人把 exclude_unset 删掉后 `is_default: false` 混进真实策略。
* `POST /intervention/requests`：自动放行（`detail`）↔ 落单待批（`request_id`+`status`）。
* `POST /intervention/requests/{id}/decide`：普通单只回 `{id, status}`；挂了高危副作用的
  单才多一个 `execution`——两个都用相等断言，这样"永远带个 null execution"会被抓住。
* `POST /experts/search`：命中是人设 + `reason`，没谈成是 `{id: null, message}`。
  这条走的是 `ExpertPickOut | ExpertSearchMissOut` 联合，所以两种形状的键集都测。

LLM 相关的两个端点（search / team）用 monkeypatch 顶掉网关的 chat 方法，
测的是**响应形状与降级分支**，不需要密钥，也不消耗任何真实调用。
"""
from __future__ import annotations

import uuid

from fastapi.routing import APIRoute, _IncludedRouter
from pydantic import BaseModel

from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.agent import Agent, AgentModelConfig, Model
from app.models.expert_template import ExpertTemplate
from app.models.intervention import ApprovalRequest
from app.schemas import responses_gate as gate

_MODULES = {"app.api.routes.intervention", "app.api.routes.experts"}

# ExpertTemplate.public_dict() 的键集合镜像：public_dict 改一处，这里立刻红。
EXPERT_PUBLIC_KEYS = {
    "id", "source", "source_url", "source_slug", "name", "emoji", "color",
    "license", "department", "tags", "description", "deliverables",
    "recommended_packs", "enabled", "hired_count", "prompt_chars",
    "has_raw_md",
}

# 种子里的人设正文：列表接口回的是它的字符数，断言要用同一个值对账
_SEED_PROMPT = "## 使命\n提升转化率。"


def _id() -> str:
    return str(uuid.uuid4())


def _keys(model) -> set[str]:
    return set(model.model_fields)


def _api_routes() -> list[APIRoute]:
    found: list[APIRoute] = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif isinstance(route, _IncludedRouter):
                walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                walk(route.routes)

    from app.main import app
    walk(app.routes)
    return found


async def _seed_expert(company_id: str, **over) -> str:
    """一个能代表真实库的记录：slug 走 {dept}-{file} 规则，正文/交付物齐活。"""
    fields = {
        "company_id": company_id,
        "source_slug": "engineering-landing-optimizer",
        "name": "转化流优化师",
        "emoji": "📈",
        "color": "#6366f1",
        "department": "engineering",
        "tags": ["工程"],
        "description": "把落地页从曝光做到成交。",
        "system_prompt": _SEED_PROMPT,
        "raw_md": "# 转化流优化师\n\n## 交付物\n- A/B 实验方案\n- 落地页改写稿",
        "deliverables": ["A/B 实验方案", "落地页改写稿"],
        "license": "AI-EOS 原创",
        "source": "ai-eos-original",
        "enabled": True,
    }
    fields.update(over)
    eid = _id()
    async with AsyncSessionLocal() as s:
        s.add(ExpertTemplate(id=eid, **fields))
        await s.commit()
    return eid


async def _seed_model_config(company_id: str) -> None:
    """search/team 端点要「至少有一个模型配置」才会往下走（否则 503）。"""
    async with AsyncSessionLocal() as s:
        m = Model(id=_id(), company_id=company_id, name="假模型",
                  provider_type="openai_compat", model_id="fake-flash")
        s.add(m)
        await s.commit()
        a = Agent(id=_id(), company_id=company_id, name="检索用员工",
                  role="测试", status="IDLE")
        s.add(a)
        await s.commit()
        s.add(AgentModelConfig(agent_id=a.id, role="primary", model_id=m.id))
        await s.commit()


def _fake_gateway(monkeypatch, content: str) -> None:
    """顶掉真实 LLM 调用：只关心端点怎么解析、怎么序列化。"""

    async def _chat(self, model_ids, messages, config=None, **kw):  # noqa: ANN001
        return {"content": content}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _chat)


_EXPERT_MD = """---
name: 落地页文案手
emoji: ✍️
color: pink
description: 写能转化的落地页文案。
---

# 落地页文案手

## 交付物
- 首屏文案 3 版
- FAQ 话术
"""


# ================================================================ intervention


async def test_policy_default_branch_and_saved_branch(client, tenants):
    """没配过策略 → `{company_id, project_id, level, approval_actions, is_default}`；
    存过之后 → `{id, company_id, project_id, level, approval_actions}`。
    两个分支的键集**不同**，这就是路由要带 exclude_unset 的原因。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]

    r = await client.get(f"/intervention/policy?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    default = r.json()
    assert set(default) == {"company_id", "project_id", "level",
                            "approval_actions", "is_default"}, default
    assert default["is_default"] is True and default["level"] == 2
    assert set(default) <= _keys(gate.InterventionPolicyOut)

    r = await client.put(f"/intervention/policy?company_id={co}",
                         json={"level": 3}, headers=h)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert set(saved) == {"id", "company_id", "project_id", "level",
                          "approval_actions"}, saved
    assert saved["level"] == 3
    # PUT 回的是真实策略行，默认分支的 is_default 不能漏进来
    assert "is_default" not in saved

    r = await client.get(f"/intervention/policy?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == set(saved), r.json()
    assert r.json()["id"] == saved["id"]

    # 越界 level 仍然 400（响应模型不能替校验干活）
    r = await client.put(f"/intervention/policy?company_id={co}",
                         json={"level": 9}, headers=h)
    assert r.status_code == 400, r.text


async def test_check_action_and_requests_two_branches(client, tenants):
    """/check 与 /requests 的自动放行分支、落单分支各自的键集。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]

    r = await client.post(f"/intervention/check?company_id={co}"
                          f"&action_type=task_create", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(gate.ActionCheckOut), r.json()
    assert r.json() == {"required": True, "level": 2}

    # L4 全自动：请求直接放行，不产生审批单
    await client.put(f"/intervention/policy?company_id={co}",
                     json={"level": 4}, headers=h)
    r = await client.post(f"/intervention/requests?company_id={co}",
                          json={"action_type": "task_create"}, headers=h)
    assert r.status_code == 200, r.text
    auto = r.json()
    assert set(auto) == {"approved", "auto", "detail"}, auto
    assert auto["approved"] is True and auto["auto"] is True
    assert set(auto) <= _keys(gate.ApprovalGateOut)

    # L0 全需确认：落单，且带 request_id + status
    await client.put(f"/intervention/policy?company_id={co}",
                     json={"level": 0}, headers=h)
    r = await client.post(f"/intervention/requests?company_id={co}",
                          json={"action_type": "artifact_approve",
                                "payload": {"artifact_id": "a-1"},
                                "reason": "交付单需要老板点头"},
                          headers=h)
    assert r.status_code == 200, r.text
    pending = r.json()
    assert set(pending) == {"approved", "auto", "request_id", "status"}, pending
    assert pending["approved"] is False and pending["status"] == "PENDING"

    r = await client.get(f"/intervention/requests?company_id={co}&status=PENDING",
                         headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1, rows
    assert set(rows[0]) == _keys(gate.ApprovalRequestOut), rows[0]
    assert rows[0]["id"] == pending["request_id"]
    assert rows[0]["action_type"] == "artifact_approve"
    assert rows[0]["requested_by"] is None
    assert rows[0]["payload"] == {"artifact_id": "a-1"}

    # force=True：策略说不用批也得落单（Agent 自己拿不准）
    await client.put(f"/intervention/policy?company_id={co}",
                     json={"level": 4}, headers=h)
    r = await client.post(f"/intervention/requests?company_id={co}",
                          json={"action_type": "task_create", "force": True},
                          headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"approved", "auto", "request_id", "status"}, r.json()


async def test_decide_plain_request_returns_no_execution_key(client, tenants):
    """普通审批单裁定：只回 `{id, status}`，不能凭空多一个 `execution: null`。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    await client.put(f"/intervention/policy?company_id={co}",
                     json={"level": 0}, headers=h)
    r = await client.post(f"/intervention/requests?company_id={co}",
                          json={"action_type": "meeting_start"}, headers=h)
    rid = r.json()["request_id"]

    r = await client.post(f"/intervention/requests/{rid}/decide",
                          json={"decision": "approve", "note": "可以开"}, headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(gate.DecideOut) - {"execution"}, r.json()
    assert r.json() == {"id": rid, "status": "APPROVED"}

    # 重复裁定 → 409（错误信封，不走 DecideOut）
    r = await client.post(f"/intervention/requests/{rid}/decide",
                          json={"decision": "reject"}, headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Already decided: APPROVED"


async def test_decide_awaiting_external_agent_carries_execution(client, tenants):
    """批准挂了外发副作用的单 → 多一个 `execution`，且形状是 {ok, result, error}。

    连接 id 指向不存在的外部连接，所以补执行必然失败——既测到 execution 分支，
    又保证测试不会真的往外发东西。
    """
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    async with AsyncSessionLocal() as s:
        req = ApprovalRequest(id="appr_" + _id()[:8], company_id=co,
                              action_type="external_agent", status="PENDING",
                              payload={"awaiting_execution": True,
                                       "connection_id": "conn_missing",
                                       "task_input": "把方案发给客户"})
        s.add(req)
        await s.commit()

    r = await client.post(f"/intervention/requests/{req.id}/decide",
                          json={"decision": "approve"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "status", "execution"}, body
    assert body["status"] == "APPROVED"
    # 失败分支只有 {ok, error}，成功分支才是 {ok, result}——两个都不许多出 null 键
    assert set(body["execution"]) == {"ok", "error"}, body
    assert set(body["execution"]) <= _keys(gate.ApprovalExecutionOut)
    assert body["execution"]["ok"] is False
    assert body["execution"]["error"] == "外部连接已删除，无法执行"


async def test_always_allow_writes_whitelist_then_revoke(client, tenants):
    """勾「始终允许」→ 白名单行出现（键集固定）→ DELETE 撤销回 `{id, deleted}`。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    async with AsyncSessionLocal() as s:
        req = ApprovalRequest(id="appr_" + _id()[:8], company_id=co,
                              action_type="external_agent", status="PENDING",
                              payload={"capability": "external_agent",
                                       "params": {"task_input": "发周报给客户"}})
        s.add(req)
        await s.commit()

    r = await client.get(f"/intervention/whitelist?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []

    r = await client.post(f"/intervention/requests/{req.id}/decide",
                          json={"decision": "approve", "always_allow": True},
                          headers=h)
    assert r.status_code == 200, r.text
    # 没有 awaiting_execution → 不补执行，回执仍是两个键
    assert set(r.json()) == {"id", "status"}, r.json()

    r = await client.get(f"/intervention/whitelist?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1, rows
    assert set(rows[0]) == _keys(gate.WhitelistOut), rows[0]
    assert rows[0]["capability"] == "external_agent"
    assert rows[0]["param_pattern"] == "发周报给客户"
    assert rows[0]["note"] == "老板批准时勾「始终允许」"

    r = await client.delete(f"/intervention/whitelist/{rows[0]['id']}"
                            f"?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": rows[0]["id"], "deleted": True}
    assert set(r.json()) <= _keys(gate.WhitelistDeletedOut)

    r = await client.delete(f"/intervention/whitelist/{rows[0]['id']}"
                            f"?company_id={co}", headers=h)
    assert r.status_code == 404, r.text


async def test_override_records_and_log_keys(client, tenants):
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    r = await client.post(f"/intervention/override?company_id={co}",
                          json={"target_type": "task", "target_id": "t" * 32,
                                "original_decision": {"status": "DONE"},
                                "override_decision": {"status": "REWORK"},
                                "reason": "验收没通过"}, headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(gate.OverrideOut), r.json()
    assert r.json()["target"] == f"task:{'t' * 8}"

    r = await client.get(f"/intervention/overrides?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1, rows
    assert set(rows[0]) == _keys(gate.OverrideLogOut), rows[0]
    assert rows[0]["original"] == {"status": "DONE"}
    assert rows[0]["override"] == {"status": "REWORK"}
    assert isinstance(rows[0]["created_at"], str)


async def test_intervention_cross_tenant_is_404(client, tenants):
    """别人的公司 id：读策略、列审批单都 404，且不泄露公司是否存在。"""
    r = await client.get(f"/intervention/policy?company_id={tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text
    r = await client.get(f"/intervention/requests?company_id={tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text
    r = await client.put(f"/intervention/policy?company_id={tenants.bob_co['id']}",
                         json={"level": 0}, headers=tenants.alice["headers"])
    assert r.status_code == 404, r.text


# ===================================================================== experts


async def test_experts_list_and_departments_keys(client, tenants):
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    await _seed_expert(co)
    await _seed_expert(co, source_slug="design-ux-hunter", name="体验捕手",
                       department="design", raw_md=None, deliverables=[])

    r = await client.get(f"/experts?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2, rows
    for row in rows:
        assert set(row) == EXPERT_PUBLIC_KEYS, row
        assert set(row) == _keys(gate.ExpertPublicOut)
    by_name = {x["name"]: x for x in rows}
    assert by_name["转化流优化师"]["has_raw_md"] is True
    assert by_name["体验捕手"]["has_raw_md"] is False
    assert by_name["体验捕手"]["deliverables"] == []
    # prompt_chars 是"正文有多长"的替代指标：它必须真的等于 system_prompt 长度，
    # 否则列表页的「查看完整档案」判断会失真
    assert by_name["转化流优化师"]["prompt_chars"] == len(_SEED_PROMPT)
    # 部门兜底的推荐技能包永远非空
    assert by_name["转化流优化师"]["recommended_packs"]

    r = await client.get(f"/experts/departments?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    depts = r.json()
    assert {d["department"] for d in depts} == {"engineering", "design"}, depts
    for d in depts:
        assert set(d) == _keys(gate.ExpertDepartmentOut), d


async def test_expert_detail_and_edit_and_copy_keys(client, tenants):
    """详情 = 列表 17 键 + raw_md + system_prompt；编辑/复制/新建回同一形状。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    eid = await _seed_expert(co)
    detail_keys = EXPERT_PUBLIC_KEYS | {"raw_md", "system_prompt"}

    r = await client.get(f"/experts/{eid}?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert set(detail) == detail_keys, detail
    assert set(detail) == _keys(gate.ExpertDetailOut)
    assert detail["raw_md"].startswith("# 转化流优化师")

    r = await client.put(f"/experts/{eid}?company_id={co}",
                         json={"raw_md": "# 转化流优化师\n\n## 交付物\n- 漏斗诊断报告\n"},
                         headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == detail_keys, r.json()
    assert r.json()["deliverables"] == ["漏斗诊断报告"], "改正文要重提交付物清单"

    r = await client.post(f"/experts/{eid}/copy?company_id={co}", headers=h)
    assert r.status_code == 201, r.text
    clone = r.json()
    assert set(clone) == detail_keys, clone
    assert clone["id"] != eid
    assert clone["name"] == "转化流优化师（副本）"
    assert clone["source_slug"].startswith("custom-copy-")

    r = await client.post(f"/experts/manual?company_id={co}",
                          json={"name": "自定义专家", "department": "marketing",
                                "system_prompt": "## 使命\n做事",
                                "attachments": ["file-1"]}, headers=h)
    assert r.status_code == 201, r.text
    manual = r.json()
    assert set(manual) == detail_keys, manual
    assert manual["source"] == "ai-eos-original"
    assert manual["emoji"] == "🧑‍💼", "缺省头像要补上，前端卡片靠它渲染"
    assert "file-1" in manual["tags"]


async def test_expert_to_blueprint_returns_job_and_new_hire_count(client, tenants):
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    eid = await _seed_expert(co, hired_count=2)

    r = await client.post(f"/experts/{eid}/to-blueprint?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(gate.ExpertBlueprintOut), body
    assert set(body["template"]) == EXPERT_PUBLIC_KEYS, body
    assert body["template"]["hired_count"] == 3, "雇佣计数要在同一份回执里可见"

    # 别人的专家：详情/雇佣都 404
    r = await client.get(f"/experts/{eid}?company_id={tenants.bob_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text


async def test_expert_search_hit_and_miss_shapes(client, tenants, monkeypatch):
    """命中带 reason，没谈成只有 {id: null, message}——两个分支键集互斥。"""
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    await _seed_expert(co)
    await _seed_model_config(co)

    _fake_gateway(monkeypatch, '好的：[{"id": 1, "reason": "做过同类落地页"}]')
    r = await client.post(f"/experts/search?company_id={co}",
                          json={"query": "帮我提转化"}, headers=h)
    assert r.status_code == 200, r.text
    hits = r.json()
    assert len(hits) == 1, hits
    assert set(hits[0]) == EXPERT_PUBLIC_KEYS | {"reason"}, hits[0]
    assert hits[0]["reason"] == "做过同类落地页"

    _fake_gateway(monkeypatch, "抱歉，我没法判断")
    r = await client.post(f"/experts/search?company_id={co}",
                          json={"query": "帮我提转化"}, headers=h)
    assert r.status_code == 200, r.text
    miss = r.json()
    assert set(miss[0]) == _keys(gate.ExpertSearchMissOut), miss
    assert miss[0]["id"] is None and miss[0]["message"]

    # 专家库为空 → 404；没模型 → 503（都不是 200 空数组，前端要能区分）
    r = await client.post(f"/experts/search?company_id={tenants.bob_co['id']}",
                          json={"query": "随便"}, headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text


async def test_expert_team_creates_blueprint_jobs(client, tenants, monkeypatch):
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    eid = await _seed_expert(co)
    await _seed_model_config(co)
    _fake_gateway(monkeypatch, '团队：[{"id": 1, "task": "负责落地页转化"}]')

    r = await client.post(f"/experts/team?company_id={co}",
                          json={"goal": "新品上市", "headcount": 1}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _keys(gate.ExpertTeamOut), body
    assert body["goal"] == "新品上市" and body["team_size"] == 1
    assert len(body["jobs"]) == 1
    assert set(body["jobs"][0]) == _keys(gate.ExpertTeamJobOut), body
    assert body["jobs"][0]["template"]["id"] == eid
    assert body["jobs"][0]["task"] == "负责落地页转化"

    async with AsyncSessionLocal() as s:
        tpl = await s.get(ExpertTemplate, eid)
        assert tpl.hired_count == 1, "组队也要计雇佣次数"


async def test_expert_import_then_original_replace_counts(client, tenants,
                                                          monkeypatch, tmp_path):
    """导入回执的计数键是前端提示语的原料：少一个键页面就打印 undefined。

    vendor → 原创替换这条链在真实库里跑过，这里用临时目录复现：
    2 个能解析 + 1 个坏编码 → 导入；原创只覆盖 1 个 → 另一个旧上游下架。
    """
    co = tenants.alice_co["id"]
    h = tenants.alice["headers"]

    vdir = tmp_path / "vendor"
    vdir.mkdir()
    (vdir / "engineering-copy-writer.md").write_text(_EXPERT_MD, encoding="utf-8")
    (vdir / "design-ux-hunter.md").write_text(_EXPERT_MD, encoding="utf-8")
    (vdir / "engineering-broken.md").write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setattr("app.api.routes.experts._vendor_path", lambda: str(vdir))

    r = await client.post(f"/experts/import?company_id={co}", headers=h)
    assert r.status_code == 201, r.text
    imported = r.json()
    assert set(imported) == _keys(gate.ExpertImportOut), imported
    assert imported == {"imported_or_updated": 2, "skipped": 1, "files": 3}

    r = await client.get(f"/experts?company_id={co}", headers=h)
    assert len(r.json()) == 2
    assert all(x["source"] == "agency-agents-zh" for x in r.json())

    odir = tmp_path / "content" / "experts"
    odir.mkdir(parents=True)
    (odir / "engineering-copy-writer.md").write_text(_EXPERT_MD, encoding="utf-8")
    monkeypatch.setattr("app.api.routes.experts._original_path", lambda: str(odir))

    r = await client.post(f"/experts/import-original?company_id={co}", headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    assert set(out) == _keys(gate.ExpertOriginalImportOut), out
    assert out["created"] == 0 and out["updated"] == 1
    assert out["disabled_legacy"] == 1, "没被原创覆盖的旧上游要下架"

    r = await client.get(f"/experts?company_id={co}", headers=h)
    rows = r.json()
    assert len(rows) == 1 and rows[0]["source"] == "ai-eos-original"
    assert rows[0]["license"] == "AI-EOS 原创"


# ================================================================= 静态护栏


def test_both_routers_declare_response_models():
    """两个路由的 2xx JSON 端点一个不许裸着——新加端点必须带模型。"""
    missing = []
    total = 0
    for route in _api_routes():
        module = getattr(route.endpoint, "__module__", "")
        if module not in _MODULES:
            continue
        for verb in route.methods:
            if verb in ("HEAD", "OPTIONS"):
                continue
            total += 1
            if route.response_model is None:
                missing.append(f"{verb} {route.path}")
    assert total >= 21, f"两个路由的操作数不对（实测 {total}）"
    assert not missing, f"这些端点没有 response_model：{missing}"


def test_gate_models_allow_unknown_fields():
    """判断错字段时宁可原样透出，也不许序列化静默裁掉前端在用的键。"""
    offenders = {
        name for name, obj in vars(gate).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel)
        and obj.__module__ == gate.__name__
        and obj.model_config.get("extra") != "allow"
    }
    assert not offenders, offenders
