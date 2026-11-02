"""CEO 身份与专属技能包：改名/还原、自动装卸、切换、启动自愈。

全部走临时 SQLite；不触碰真实库，也不调用模型。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, text

from app.ceo_skill_bundle import (CEO_SKILLS, ensure_ceo_bundles)
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.skill import (AgentSkill, Skill as SkillRow,
                              SkillStatus)
from app.schemas.responses_ceo import CeoReleaseOut


async def _seed_agent(company_id: str, user_id: str, name: str,
                      role: str = "专家") -> str:
    agent_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=agent_id, company_id=company_id, name=name, role=role,
                    status=AgentStatus.IDLE, created_by_user_id=user_id))
        await s.commit()
    return agent_id


async def _skill_bindings(agent_id: str) -> dict[str, str | None]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(SkillRow.slug, AgentSkill.source)
            .join(AgentSkill, AgentSkill.skill_id == SkillRow.id)
            .where(AgentSkill.agent_id == agent_id))).all()
    return {slug: source for slug, source in rows}


async def test_assign_renames_and_binds_bundle_then_release_restores(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    aid = await _seed_agent(co["id"], alice["id"], "王总", "总经理")
    # 手工装的技能必须在解除 CEO 后保留
    r = await client.post("/skills/web_search/install",
                          json={"agent_id": aid}, headers=alice["headers"])
    assert r.status_code in (200, 201), r.text

    assigned = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": aid}, headers=alice["headers"])
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["ceo_agent_name"] == "CEO"

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, aid)
        assert agent.name == "CEO"
        assert agent.pre_ceo_name == "王总"
        assert agent.is_ceo is True

    bound = await _skill_bindings(aid)
    ceo_slugs = {spec["slug"] for spec in CEO_SKILLS}
    assert ceo_slugs <= set(bound), sorted(ceo_slugs - set(bound))
    assert all(bound[slug] == "ceo_bundle" for slug in ceo_slugs)
    assert bound.get("web_search") == "manual"

    # 重复任命：不重复绑定，也不把「CEO」写进原名备份
    again = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": aid}, headers=alice["headers"])
    assert again.status_code == 200, again.text
    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, aid)
        assert agent.pre_ceo_name == "王总"
    assert len(await _skill_bindings(aid)) == len(ceo_slugs) + 1

    released = await client.post("/ceo/config/release", json={
        "company_id": co["id"], "agent_id": aid}, headers=alice["headers"])
    assert released.status_code == 200, released.text
    assert released.json()["restored_name"] == "王总"

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, aid)
        assert agent.name == "王总"
        assert agent.is_ceo is False
        assert agent.pre_ceo_name is None
    bound = await _skill_bindings(aid)
    assert not (ceo_slugs & set(bound)), sorted(ceo_slugs & set(bound))
    assert bound.get("web_search") == "manual"

    cfg = await client.get("/ceo/config", params={"company_id": co["id"]},
                           headers=alice["headers"])
    assert cfg.json()["has_ceo"] is False


async def test_switching_ceo_releases_previous_identity_and_skills(
        client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    first = await _seed_agent(co["id"], alice["id"], "王总", "总经理")
    second = await _seed_agent(co["id"], alice["id"], "小李", "运营")

    r = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": first}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    r = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": second}, headers=alice["headers"])
    assert r.status_code == 200, r.text

    async with AsyncSessionLocal() as s:
        old = await s.get(Agent, first)
        new = await s.get(Agent, second)
        assert old.is_ceo is False and old.name == "王总"
        assert new.is_ceo is True and new.name == "CEO"
        assert new.pre_ceo_name == "小李"
    assert not (await _skill_bindings(first))
    assert len(await _skill_bindings(second)) == len(CEO_SKILLS)


async def test_startup_self_heal_renames_legacy_ceo_and_binds_bundle(tenants):
    alice, co = tenants.alice, tenants.alice_co
    aid = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=aid, company_id=co["id"], name="老张", role="总经理",
                    is_ceo=True, status=AgentStatus.IDLE,
                    created_by_user_id=alice["id"]))
        await s.commit()

        info = await ensure_ceo_bundles(s)
        await s.commit()

        assert info["agents"] >= 1
        agent = await s.get(Agent, aid)
        assert agent.name == "CEO"
        assert agent.pre_ceo_name == "老张"
    bound = await _skill_bindings(aid)
    assert {spec["slug"] for spec in CEO_SKILLS} <= set(bound)


async def test_startup_self_heal_adopts_legacy_role_hint_ceo(tenants):
    """升级前的老 CEO 只有岗位名、没有 is_ceo：启动后要正式收编、改名、装包。"""
    alice, co = tenants.alice, tenants.alice_co
    aid = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=aid, company_id=co["id"], name="测试负责人", role="内容合规",
                    status=AgentStatus.IDLE, created_by_user_id=alice["id"]))
        await s.commit()
        # 模拟老库：升级前 is_ceo 列是后加的，历史行是 NULL 而不是 False
        await s.execute(text("UPDATE agents SET is_ceo = NULL WHERE id = :id"),
                       {"id": aid})
        await s.commit()

        await ensure_ceo_bundles(s)
        await s.commit()

        agent = await s.get(Agent, aid)
        assert agent.is_ceo is True
        assert agent.name == "CEO"
        assert agent.pre_ceo_name == "测试负责人"
    bound = await _skill_bindings(aid)
    assert {spec["slug"] for spec in CEO_SKILLS} <= set(bound)


async def test_startup_self_heal_keeps_explicitly_released_ceo_released(tenants):
    """老板解除后 is_ceo=False 是显式状态，不能再按岗位名认回来。"""
    alice, co = tenants.alice, tenants.alice_co
    aid = await _seed_agent(co["id"], alice["id"], "王总", "总经理")
    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, aid)
        agent.is_ceo = False
        await s.commit()

        await ensure_ceo_bundles(s)
        await s.commit()

        agent = await s.get(Agent, aid)
        assert agent.is_ceo is False
        assert agent.name == "王总"
    assert not (await _skill_bindings(aid))


async def test_system_exclusive_skills_are_not_auto_provisioned(
        client, make_user, make_company):
    """系统级专属技能（CEO 包那一类）只能显式绑定；公司自建技能照常按岗位匹配。"""
    user = await make_user()
    co = await make_company(user)

    pub = await client.post(f"/skills/custom?company_id={co['id']}",
                            json={"name": "量子对账公开版", "scope": "public",
                                  "description": "量子对账专用方法论",
                                  "instruction": "量子对账：先核对口径，再复算差异"},
                            headers=user["headers"])
    assert pub.status_code == 201, pub.text

    sys_exc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(SkillRow(id=sys_exc_id, slug=f"sys_exc_{uuid.uuid4().hex[:8]}",
                       name="量子对账系统专属版", company_id=None,
                       scope="exclusive", source="builtin",
                       status=SkillStatus.ENABLED,
                       description="量子对账专用方法论",
                       input_schema={"instruction": "量子对账：系统专属"}))
        await s.commit()

    from app.skill_provisioning import local_skill_matches
    async with AsyncSessionLocal() as s:
        rows = await local_skill_matches(s, co["id"], "量子对账", limit=20)
    ids = {row.id for row in rows}
    assert pub.json()["id"] in ids
    assert sys_exc_id not in ids


async def test_release_contract_and_cross_tenant_guard(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    aid = await _seed_agent(co["id"], alice["id"], "王总", "总经理")
    r = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": aid}, headers=alice["headers"])
    assert r.status_code == 200, r.text

    # 非 CEO 解除 → 409，不能靠重复点击把状态搞乱
    other = await _seed_agent(co["id"], alice["id"], "小张")
    r = await client.post("/ceo/config/release", json={
        "company_id": co["id"], "agent_id": other}, headers=alice["headers"])
    assert r.status_code == 409

    # 跨租户 → 404（不泄露他人公司里谁当 CEO）
    r = await client.post("/ceo/config/release", json={
        "company_id": co["id"], "agent_id": aid},
        headers=tenants.bob["headers"])
    assert r.status_code == 404

    r = await client.post("/ceo/config/release", json={
        "company_id": co["id"], "agent_id": aid}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) == set(CeoReleaseOut.model_fields)
