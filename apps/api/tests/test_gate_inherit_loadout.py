"""M6：角色档案出厂闸门继承 + CEO 统筹接阵容。

两条都是"少抄一遍、少编一个"的机制，不改变老数据的语义：
- 继承只给**没写 gate** 的 AGENT 节点补，且强制观察态（不阻断生产）；
- 统筹带 loadout 时才锁人，NULL 的统筹行为与以前完全一致。
"""
import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.gates.inherit import sanitize_default_gate
from app.models.agent import Agent
from app.models.team import RoleProfile, TeamLoadout
from app.models.workflow import WorkflowDefinition, WorkflowNode
from app.workflow_authoring import _loadout_agent_ids


def spec_one_agent(name: str, gate: dict | None = None) -> dict:
    node = {"key": "n1", "type": "AGENT", "name": "写报告",
            "agent": name, "task": "写一份报告"}
    if gate:
        node["gate"] = gate
    return {"spec": "v1", "key": "inherit-test", "name": "继承测试",
            "nodes": [{"key": "start", "type": "START", "name": "开始"},
                      node,
                      {"key": "end", "type": "END", "name": "结束"}],
            "edges": [{"from": "start", "to": "n1"},
                      {"from": "n1", "to": "end"}]}


def test_sanitize_forces_observe_and_drops_unknown_rules():
    gate = sanitize_default_gate({
        "acceptance": "要有结论", "on_fail": "fail", "max_rework": 3,
        "assert": {"min_chars": 500, "min_words": 10},   # 后者是不存在的规则
    })
    assert gate["on_fail"] == "observe"
    assert gate["max_rework"] == 0
    assert gate["assert"] == {"min_chars": 500}
    assert gate["acceptance"] == "要有结论"

    # 只有 acceptance 也算可用；啥都没有返回 None（等于不继承）
    assert sanitize_default_gate({"acceptance": "x"})["on_fail"] == "observe"
    assert sanitize_default_gate({"assert": {"min_words": 3}}) is None
    assert sanitize_default_gate(None) is None


@pytest.mark.asyncio
async def test_import_inherits_role_default_gate(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h, base = user["headers"], f"?company_id={co['id']}"

    await client.post(f"/role-profiles{base}", headers=h, json={
        "key": "analyst", "name": "分析师",
        "default_gate": {"acceptance": "数字要有出处",
                         "assert": {"min_citations": 2}}})
    async with AsyncSessionLocal() as db:
        a = Agent(company_id=co["id"], name="分析师")
        db.add(a)
        await db.flush()
        p = (await db.execute(
            select(RoleProfile).where(
                RoleProfile.company_id == co["id"]))).scalars().first()
        a.role_profile_id = p.id
        await db.commit()

    r = await client.post(f"/workflows/import{base}", headers=h,
                          json={"spec": spec_one_agent("分析师")})
    assert r.status_code == 200
    assert r.json()["gates_inherited"] == 1

    async with AsyncSessionLocal() as db:
        wf = await db.get(WorkflowDefinition, r.json()["id"])
        node = (await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_id == wf.id,
                WorkflowNode.node_key == "n1"))).scalars().first()
        gate = node.config["gate"]
        assert gate["on_fail"] == "observe" and gate["max_rework"] == 0
        assert gate["assert"] == {"min_citations": 2}


@pytest.mark.asyncio
async def test_explicit_gate_wins_and_missing_profile_does_nothing(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h, base = user["headers"], f"?company_id={co['id']}"

    # 员工没有角色档案 → 不继承，gates_inherited=0，节点也不该凭空多出 gate
    async with AsyncSessionLocal() as db:
        a = Agent(company_id=co["id"], name="无档案员工")
        db.add(a)
        await db.commit()

    r = await client.post(f"/workflows/import{base}", headers=h,
                          json={"spec": spec_one_agent("无档案员工")})
    assert r.status_code == 200 and r.json()["gates_inherited"] == 0
    async with AsyncSessionLocal() as db:
        nodes = (await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_id == r.json()["id"]))).scalars().all()
        assert all("gate" not in (n.config or {}) for n in nodes)

    # 显式 gate 优先：导入不覆盖，也不改它的 on_fail
    r2 = await client.post(f"/workflows/import{base}", headers=h,
                           json={"spec": spec_one_agent(
                               "无档案员工",
                               gate={"assert": {"min_chars": 100}, "on_fail": "fail"})})
    assert r2.status_code == 200 and r2.json()["gates_inherited"] == 0
    async with AsyncSessionLocal() as db:
        node = (await db.execute(
            select(WorkflowNode).where(
                WorkflowNode.workflow_id == r2.json()["id"],
                WorkflowNode.node_key == "n1"))).scalars().first()
        assert node.config["gate"]["on_fail"] == "fail"


@pytest.mark.asyncio
async def test_loadout_agent_ids_binds_profiles_and_names(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as db:
        p = RoleProfile(company_id=co["id"], key="writer", name="文案")
        db.add(p)
        await db.flush()
        bound = Agent(company_id=co["id"], name="小文", role_profile_id=p.id)
        named = Agent(company_id=co["id"], name="文案")   # 未绑档案，用名字兜底
        outsider = Agent(company_id=co["id"], name="外部顾问")
        db.add_all([bound, named, outsider])
        await db.flush()
        t = TeamLoadout(company_id=co["id"], name="写作阵容",
                        roles=[{"key": "writer", "name": "文案"}])
        db.add(t)
        await db.commit()
        ids = await _loadout_agent_ids(db, co["id"], t.id)
        assert {bound.id, named.id} <= ids
        assert outsider.id not in ids

    # 阵容空/角色没落地员工 → 明确报错，不返回空集合让调用方"静默放行"
    async with AsyncSessionLocal() as db:
        t2 = TeamLoadout(company_id=co["id"], name="空阵容", roles=[])
        db.add(t2)
        await db.commit()
        with pytest.raises(ValueError):
            await _loadout_agent_ids(db, co["id"], t2.id)
