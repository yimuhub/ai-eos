"""自动组织架构生成：资料覆盖度、假模型结构化生成、落地幂等、回滚与租户隔离。"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.agent import Agent, AgentModelConfig, Model, ModelProviderType
from app.models.company import Department
from app.models.knowledge import KBDocument, KBDocumentStatus
from app.models.org_synthesis import OrgSynthesisRun, OrgSynthesisStatus

DOCS = [
    ("公司介绍与业务说明", "我们是一家为中小企业提供 AI 客服 SaaS 的公司，主营智能客服与知识库搭建，按年订阅收费。"),
    ("客户与市场", "客户分为电商、教育、本地生活三类；获客渠道主要是内容营销与渠道代理，销售按行业分组。"),
    ("交付流程与验收", "交付流程：需求确认 → 知识库配置 → 上线试运行 → 验收；售后由客户成功跟进，SOP 见附件。"),
    ("团队与岗位", "现有团队 8 人，岗位包括产品经理、内容编辑、销售与实施顾问，汇报给总经理。"),
]

PLAN = {
    "company_profile": {
        "industry": "企业服务/SaaS",
        "business": "AI 客服 SaaS，按年订阅",
        "stage": "成长期",
        "scale_hint": "10 人以内",
        "summary": "面向中小企业的 AI 客服 SaaS 公司，需要产品、内容与增长三类能力。",
    },
    "centers": [
        {"name": "产品中心", "description": "产品与内容生产"},
        {"name": "增长中心", "description": "获客与销售"},
    ],
    "departments": [
        {"name": "产品部", "center": "产品中心", "description": "产品规划与迭代"},
        {"name": "内容部", "center": "产品中心", "description": "内容与知识库素材"},
        {"name": "市场部", "center": "增长中心", "description": "投放与获客"},
        {"name": "销售部", "center": "增长中心", "description": "线索转化与签约"},
    ],
    "agents": [
        {"name": "产品经理", "role": "产品规划与需求管理", "department": "产品部",
         "headcount": 1, "is_leader": True, "reason": "资料提到产品迭代与知识库配置",
         "evidence": ["公司介绍与业务说明"], "system_prompt": ""},
        {"name": "内容编辑", "role": "知识库与内容生产", "department": "内容部",
         "headcount": 1, "is_leader": True, "reason": "获客依赖内容营销",
         "evidence": ["客户与市场"], "system_prompt": "你负责内容生产。"},
        {"name": "投放优化师", "role": "投放与线索获取", "department": "市场部",
         "headcount": 1, "is_leader": True, "reason": "客户资料提到多渠道获客",
         "evidence": ["客户与市场"], "system_prompt": ""},
        {"name": "销售", "role": "线索跟进与签约", "department": "销售部",
         "headcount": 2, "is_leader": True, "reason": "销售按行业分组",
         "evidence": ["客户与市场", "团队与岗位"], "system_prompt": ""},
    ],
    "warnings": [
        {"level": "medium", "title": "销售编制待确认", "detail": "资料只写了按行业分组，没有给出具体人数。"},
    ],
}


def _fake_llm(content: str | None = None, usage: dict | None = None):
    async def _chat(self, model_ids, messages, tools=None, config=None):
        body = content if content is not None else json.dumps(
            PLAN, ensure_ascii=False)
        return {
            "content": body,
            "usage": usage or {"prompt_tokens": 1200,
                               "completion_tokens": 800,
                               "total_tokens": 2000},
            "finish_reason": "stop",
        }

    return _chat


async def _seed(company_id: str, docs: bool = True) -> str:
    async with AsyncSessionLocal() as db:
        model = Model(company_id=None, name="测试对话模型",
                      provider_type=ModelProviderType.OPENAI_COMPAT,
                      model_id="test-chat", model_type="text")
        db.add(model)
        if docs:
            for title, content in DOCS:
                db.add(KBDocument(
                    id=str(uuid.uuid4()), company_id=company_id, title=title,
                    source_type="text", content=content,
                    status=KBDocumentStatus.READY, chunk_count=1,
                    keywords=["测试"]))
        await db.commit()
        return str(model.id)


async def _db_counts(company_id: str) -> tuple[int, int, int]:
    async with AsyncSessionLocal() as db:
        departments = (await db.execute(
            select(func.count()).select_from(Department)
            .where(Department.company_id == company_id))).scalar_one()
        agents = (await db.execute(
            select(func.count()).select_from(Agent)
            .where(Agent.company_id == company_id))).scalar_one()
        bindings = (await db.execute(
            select(func.count()).select_from(AgentModelConfig))).scalar_one()
        return departments, agents, bindings


@pytest.mark.asyncio
async def test_readiness_names_missing_dimensions_and_tracks_coverage(
        client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]

    empty = await client.get("/org-synthesis/readiness",
                             params={"company_id": company_id},
                             headers=alice["headers"])
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["score"] == 0
    assert body["level"] == "low"
    assert {m["key"] for m in body["missing"]} == {
        "business", "customer", "process", "team"}

    await _seed(company_id)
    full = await client.get("/org-synthesis/readiness",
                            params={"company_id": company_id},
                            headers=alice["headers"])
    assert full.status_code == 200, full.text
    body = full.json()
    assert body["document_count"] == 4
    assert body["ready_count"] == 4
    assert body["missing"] == []
    assert body["score"] >= 75
    assert all(dim["covered"] for dim in body["dimensions"])


@pytest.mark.asyncio
async def test_analyze_builds_reviewable_draft(client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    model_id = await _seed(company_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_llm())

    r = await client.post("/org-synthesis/analyze",
                          json={"company_id": company_id, "model_id": model_id},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "READY"
    assert run["can_apply"] is True
    assert run["model_id"] == model_id
    assert [s["key"] for s in run["stages"]] == [
        "inventory", "profile", "design", "review"]
    assert all(s["duration_ms"] >= 0 for s in run["stages"])

    draft = run["draft"]
    assert draft["totals"] == {
        "centers": 2, "departments": 4, "roles": 4,
        "headcount": 5, "new_headcount": 5, "reuse_headcount": 0,
    }
    assert draft["profile"]["industry"] == "企业服务/SaaS"
    sales = next(a for a in draft["agents"] if a["name"] == "销售")
    assert sales["headcount"] == 2
    assert sales["state"] == "new"
    assert sales["evidence"] == ["客户与市场", "团队与岗位"]
    # 模型没写人设时按资料兜底，不能留空
    assert all(a["system_prompt"].strip() for a in draft["agents"])
    # 可复算的交叉审查必须补上模型没提的缺口
    titles = {w["title"] for w in draft["warnings"]}
    assert "销售编制待确认" in titles
    assert any("财务" in t for t in titles)
    assert run["usage"]["total_tokens"] == 2000
    assert run["usage"]["cost_estimate_cny"] == 0.004


@pytest.mark.asyncio
async def test_analyze_without_model_returns_actionable_error(
        client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_llm())

    r = await client.post("/org-synthesis/analyze",
                          json={"company_id": company_id},
                          headers=alice["headers"])
    assert r.status_code == 400, r.text
    assert "模型" in r.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_json_marks_run_failed(client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    model_id = await _seed(company_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models",
                        _fake_llm("这不是 JSON，只是一段解释"))

    r = await client.post("/org-synthesis/analyze",
                          json={"company_id": company_id, "model_id": model_id},
                          headers=alice["headers"])
    assert r.status_code == 502, r.text
    assert "JSON" in r.json()["detail"]

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(OrgSynthesisRun).where(
            OrgSynthesisRun.company_id == company_id))).scalars().one()
        assert run.status == OrgSynthesisStatus.FAILED
        assert run.stage == "failed"
        assert "JSON" in (run.error or "")


@pytest.mark.asyncio
async def test_apply_is_idempotent_and_reuses_existing_org(
        client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    model_id = await _seed(company_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_llm())

    first = await client.post(
        "/org-synthesis/analyze",
        json={"company_id": company_id, "model_id": model_id},
        headers=alice["headers"])
    assert first.status_code == 200, first.text
    run_id = first.json()["id"]

    applied = await client.post(
        f"/org-synthesis/runs/{run_id}/apply",
        json={"model_id": model_id}, headers=alice["headers"])
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["created_centers"] == 2
    assert result["created_departments"] == 6  # 2 中心 + 4 部门
    assert result["created_agents"] == 5
    assert result["reused_headcount"] == 0

    departments, agents, bindings = await _db_counts(company_id)
    assert (departments, agents, bindings) == (6, 5, 5)

    async with AsyncSessionLocal() as db:
        dept = (await db.execute(select(Department).where(
            Department.company_id == company_id,
            Department.name == "产品部"))).scalars().one()
        assert dept.parent_department_id
        leader = (await db.execute(select(Agent).where(
            Agent.department_id == dept.id,
            Agent.name == "产品经理"))).scalars().one()
        assert dept.manager_agent_id == leader.id
        assert leader.system_prompt
        assert leader.memory_config["run_id"] == run_id

    again = await client.post(
        f"/org-synthesis/runs/{run_id}/apply", json={},
        headers=alice["headers"])
    assert again.status_code == 409

    second = await client.post(
        "/org-synthesis/analyze",
        json={"company_id": company_id, "model_id": model_id},
        headers=alice["headers"])
    assert second.status_code == 200, second.text
    draft = second.json()["draft"]
    assert draft["totals"]["new_headcount"] == 0
    assert draft["totals"]["reuse_headcount"] == 5
    assert {a["state"] for a in draft["agents"]} == {"reuse"}

    second_apply = await client.post(
        f"/org-synthesis/runs/{second.json()['id']}/apply", json={},
        headers=alice["headers"])
    assert second_apply.status_code == 200, second_apply.text
    second_result = second_apply.json()
    assert second_result["created_agents"] == 0
    assert second_result["created_departments"] == 0
    assert second_result["reused_headcount"] == 5

    departments, agents, _ = await _db_counts(company_id)
    assert (departments, agents) == (6, 5)


@pytest.mark.asyncio
async def test_apply_selection_then_rollback_removes_only_own_rows(
        client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    model_id = await _seed(company_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_llm())

    analyzed = await client.post(
        "/org-synthesis/analyze",
        json={"company_id": company_id, "model_id": model_id},
        headers=alice["headers"])
    run_id = analyzed.json()["id"]

    applied = await client.post(
        f"/org-synthesis/runs/{run_id}/apply",
        json={"agents": [{"name": "销售", "department": "销售部",
                          "headcount": 3}]},
        headers=alice["headers"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["created_agents"] == 3
    assert (await _db_counts(company_id))[1] == 3

    rolled = await client.post(f"/org-synthesis/runs/{run_id}/rollback",
                               headers=alice["headers"])
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()
    assert body["removed_agents"] == 3
    assert body["removed_departments"] == 6
    assert (await _db_counts(company_id))[:2] == (0, 0)

    fetched = await client.get(f"/org-synthesis/runs/{run_id}",
                               headers=alice["headers"])
    assert fetched.json()["status"] == "ROLLED_BACK"
    assert fetched.json()["snapshot"]["rolled_back_at"]


@pytest.mark.asyncio
async def test_cross_tenant_run_is_hidden(client, tenants, monkeypatch):
    alice, bob = tenants.alice, tenants.bob
    company_id = tenants.alice_co["id"]
    model_id = await _seed(company_id)
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", _fake_llm())

    analyzed = await client.post(
        "/org-synthesis/analyze",
        json={"company_id": company_id, "model_id": model_id},
        headers=alice["headers"])
    run_id = analyzed.json()["id"]

    peek = await client.get(f"/org-synthesis/runs/{run_id}",
                            headers=bob["headers"])
    assert peek.status_code == 404
    apply = await client.post(f"/org-synthesis/runs/{run_id}/apply", json={},
                              headers=bob["headers"])
    assert apply.status_code == 404
    readiness = await client.get("/org-synthesis/readiness",
                                 params={"company_id": company_id},
                                 headers=bob["headers"])
    assert readiness.status_code == 404
