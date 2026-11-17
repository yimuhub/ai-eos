"""行业专家包与访谈自动完善（V6.2）。

钉住的契约：
1. 内置目录可扫描、内容校验（三包全过）；
2. 超管解锁 = 写入公司 expert_templates（幂等；普通用户 403；错密钥 403）；
3. 访谈：登记→总控提炼（mock 模型）→技能合并去重/人设增强/留痕→
   同步已上岗 Agent；坏输出落 failed 账；
4. 访谈必须指定目标专家；租户隔离。
"""
from __future__ import annotations

import json
import uuid

from app.services.expert_pack_service import list_expert_packs


async def _unlock(client, super_headers, pack_id, company_id):
    return await client.post(
        "/expert-packs/admin/unlock",
        headers={**super_headers, **_key()},
        json={"pack_id": pack_id, "company_id": company_id})


def _key():
    from app.core.config import settings
    return {"X-Super-Key": settings.super_key}


async def _make_superadmin(make_user) -> dict:
    user = await make_user()
    from app.db.session import AsyncSessionLocal
    from app.models.user import User as UserRow
    async with AsyncSessionLocal() as db:
        row = await db.get(UserRow, user["id"])
        row.is_superadmin = True
        await db.commit()
    return user


def test_builtin_expert_packs_load():
    packs = list_expert_packs()
    ids = {p["id"] for p in packs}
    assert {"agriculture", "manufacturing", "ecommerce"} <= ids
    agri = next(p for p in packs if p["id"] == "agriculture")
    cats = {e["category"] for e in agri["experts"]}
    assert {"小麦", "玉米", "土豆", "大豆"} <= cats, "用户点名的四个细分必须在内"
    for e in agri["experts"]:
        assert 3 <= len(e["skills"]) <= 12
        assert e["system_prompt"]


async def test_unlock_writes_company_templates_and_is_idempotent(
        client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    co = tenants.alice_co["id"]
    r = await _unlock(client, admin["headers"], "agriculture", co)
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert created >= 5
    # 幂等：再次解锁不再新增
    r = await _unlock(client, admin["headers"], "agriculture", co)
    assert r.json()["created"] == 0
    # 目录接口显示解锁状态
    r = await client.get(f"/expert-packs?company_id={co}",
                         headers=tenants.alice["headers"])
    pack = next(p for p in r.json()["packs"] if p["id"] == "agriculture")
    assert pack["seeded_in_company"] == created


async def test_unlock_requires_admin_and_key(client, tenants):
    r = await client.post("/expert-packs/admin/unlock",
                          headers=tenants.alice["headers"],
                          json={"pack_id": "agriculture",
                                "company_id": tenants.alice_co["id"]})
    assert r.status_code == 403  # 非超管
    r = await client.post("/expert-packs/admin/unlock",
                          headers={**tenants.alice["headers"], **_key()},
                          json={"pack_id": "agriculture",
                                "company_id": tenants.alice_co["id"]})
    assert r.status_code == 403  # 非超管（密钥对也不行）


async def test_interview_digest_merges_skills(client, tenants, make_user,
                                              monkeypatch):
    admin = await _make_superadmin(make_user)
    co = tenants.alice_co["id"]
    await _unlock(client, admin["headers"], "agriculture", co)
    # 找小麦专家的模板 id
    r = await client.get("/expert-packs?company_id=" + co,
                         headers=tenants.alice["headers"])
    pack = next(p for p in r.json()["packs"] if p["id"] == "agriculture")
    wheat = next(e for e in pack["experts"] if e["category"] == "小麦")

    from app.db.session import AsyncSessionLocal
    from app.models.expert_template import ExpertTemplate
    async with AsyncSessionLocal() as db:
        tpl = (await db.execute(
            select(ExpertTemplate).where(
                ExpertTemplate.company_id == co,
                ExpertTemplate.source_slug == f"agriculture:{wheat['id']}")
        )).scalars().first()
        tpl_id = tpl.id
        before_skills = len(tpl.skills or [])

    transcript = ("我是负责两千亩小麦的农户技术员。我们这里每年三月中下旬"
                  "赤霉病高发，惯例是见花打药、五到七天后再补一次。"
                  "返青水不能早浇，要看分蘖和地温。另外我们用无人机做一喷三防，"
                  "一亩地药液量要控制在三十公斤以上。")
    digest_payload = {
        "summary": "补充了赤霉病防治节奏与一喷三防作业参数",
        "new_skills": [
            {"name": "赤霉病防控节奏", "description": "见花打药、间隔 5-7 天补防一次，连阴雨提前抢防",
             "category": "植保", "level": 4},
            {"name": "无人机一喷三防", "description": "亩药液量 ≥30kg，避开花期授粉时段作业",
             "category": "植保", "level": 4},
        ],
        "prompt_additions": ["给方案前先问种植规模、地区与生育期，再给用量与时机。"],
        "notes": "来源：两千亩农户技术员访谈",
    }

    # 公司需要有模型配置（总控提炼的取数前提）
    from app.models.agent import Agent, AgentModelConfig
    from app.models.agent import Model, ModelProviderType
    async with AsyncSessionLocal() as db:
        model = Model(company_id=co, name="测试模型",
                      provider_type=ModelProviderType.OPENAI_COMPAT,
                      model_id="test-model-v1")
        db.add(model)
        await db.flush()
        agent = Agent(company_id=co, name="小麦专家·上岗")
        db.add(agent)
        await db.flush()
        db.add(AgentModelConfig(agent_id=agent.id, role="primary",
                                model_id=model.id))
        await db.commit()
        agent_id = agent.id

    from app.model_gateway import ModelGateway
    async def fake_chat(self, model_ids, messages, tools=None):
        return {"content": json.dumps(digest_payload, ensure_ascii=False)}
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)

    r = await client.post(
        f"/interviews?company_id={co}", headers=tenants.alice["headers"],
        json={"transcript": transcript, "expert_template_id": tpl_id,
              "agent_id": agent_id, "department": "种植部",
              "interviewee": "技术员小王"})
    assert r.status_code == 201, r.text
    iid = r.json()["interview_id"]

    r = await client.post(
        f"/interviews/{iid}/digest?company_id={co}&sync_agent=true",
        headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied"
    assert body["digest"]["applied"]["skills_added"] == 2
    # 已上岗 Agent 的人设被同步
    async with AsyncSessionLocal() as db:
        ag = await db.get(Agent, agent_id)
    assert "来自一线访谈" in (ag.system_prompt or "")

    async with AsyncSessionLocal() as db:
        tpl = await db.get(ExpertTemplate, tpl_id)
    names = {s["name"] for s in tpl.skills}
    assert "赤霉病防控节奏" in names and "无人机一喷三防" in names
    assert len(tpl.skills) == before_skills + 2  # 合并且无重复
    assert "来自一线访谈" in tpl.system_prompt
    assert tpl.interview_history and tpl.interview_history[-1]["skills_added"] == 2

    # 访谈账：status=applied
    r = await client.get(
        f"/interviews?company_id={co}&expert_template_id={tpl_id}",
        headers=tenants.alice["headers"])
    assert r.json()["interviews"][0]["status"] == "applied"
    # bob 看不到 alice 的访谈
    r = await client.get(f"/interviews?company_id={co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


from sqlalchemy import select  # noqa: E402


async def test_interview_bad_model_output_lands_failed(client, tenants,
                                                       make_user,
                                                       monkeypatch):
    admin = await _make_superadmin(make_user)
    co = tenants.alice_co["id"]
    await _unlock(client, admin["headers"], "agriculture", co)
    r = await client.get("/expert-packs?company_id=" + co,
                         headers=tenants.alice["headers"])
    pack = next(p for p in r.json()["packs"] if p["id"] == "agriculture")
    corn = next(e for e in pack["experts"] if e["category"] == "玉米")
    from app.db.session import AsyncSessionLocal
    from app.models.expert_template import ExpertTemplate
    async with AsyncSessionLocal() as db:
        tpl = (await db.execute(
            select(ExpertTemplate).where(
                ExpertTemplate.company_id == co,
                ExpertTemplate.source_slug == f"agriculture:{corn['id']}")
        )).scalars().first()
        tpl_id = tpl.id

    from app.models.agent import Agent, AgentModelConfig
    from app.models.agent import Model, ModelProviderType
    async with AsyncSessionLocal() as db:
        model = Model(company_id=co, name="测试模型",
                      provider_type=ModelProviderType.OPENAI_COMPAT,
                      model_id="test-model-v1")
        db.add(model)
        await db.flush()
        agent = Agent(company_id=co, name="玉米专家·上岗")
        db.add(agent)
        await db.flush()
        db.add(AgentModelConfig(agent_id=agent.id, role="primary",
                                model_id=model.id))
        await db.commit()

    from app.model_gateway import ModelGateway
    async def bad_chat(self, model_ids, messages, tools=None):
        return {"content": "这不是 JSON，模型跑题了"}
    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", bad_chat)

    r = await client.post(
        f"/interviews?company_id={co}", headers=tenants.alice["headers"],
        json={"transcript": "玉米种植访谈内容，长度需要超过二十个字以上才行。",
              "expert_template_id": tpl_id})
    iid = r.json()["interview_id"]
    r = await client.post(
        f"/interviews/{iid}/digest?company_id={co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and "JSON" in body["error"]
    # 专家未被半途改写
    async with AsyncSessionLocal() as db:
        tpl = await db.get(ExpertTemplate, tpl_id)
    assert "来自一线访谈" not in (tpl.system_prompt or "")
