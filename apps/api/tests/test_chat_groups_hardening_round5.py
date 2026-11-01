"""阶段 0 安全止血 Round5：内置群聊 ensure 的幂等、配额与并发锁。

钉住三件事：

1. 同一公司连续 ensure 同一内置分组，只产生一个 group，员工不重复；
2. 配额小于预设人数时 409，且一个员工都不落库（审批单照常生成）；
3. 配额恰好够用时 ensure 成功，返回成员齐全且与库中 agent_ids 一致。

并发真锁用例未写入：两个请求同时打同一 (company, preset) 的时序在
ASGITransport + SQLite 下容易 flaky，锁 + 唯一约束的行为已由上面
连续调用与代码路径覆盖。数据库一律走 conftest 的临时 SQLite。
"""
import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.chat_group import ChatGroup
from app.models.intervention import ApprovalRequest
from app.models.membership import CompanyMember

ENSURE_URL = "/chat-groups/preset/marketing/ensure"


async def _set_owner_quota(user_id: str, company_id: str, quota: int) -> None:
    """改 owner 的个人配额（_resolve_agent_quota 的第一优先级）。"""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(CompanyMember).where(
            CompanyMember.company_id == company_id,
            CompanyMember.user_id == user_id))).scalars().first()
        assert row is not None, "建公司时应写入 owner 的成员镜像行"
        row.agent_quota = quota
        await s.commit()


async def _rows(model, company_id: str) -> list:
    async with AsyncSessionLocal() as s:
        return list((await s.execute(select(model).where(
            model.company_id == company_id))).scalars().all())


@pytest.mark.asyncio
async def test_ensure_twice_creates_one_group_and_no_duplicate_agents(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    body = {"company_id": cid}

    r1 = await client.post(ENSURE_URL, json=body, headers=t.alice["headers"])
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert len(first["created"]) == 5
    assert len(first["group"]["members"]) == 5

    r2 = await client.post(ENSURE_URL, json=body, headers=t.alice["headers"])
    assert r2.status_code == 200, r2.text
    second = r2.json()
    assert second["created"] == []
    assert second["group"]["id"] == first["group"]["id"]
    assert len(second["group"]["members"]) == 5

    groups = await _rows(ChatGroup, cid)
    agents = await _rows(Agent, cid)
    assert len(groups) == 1
    assert groups[0].preset_id == "marketing"
    assert len(agents) == 5
    assert len({a.name for a in agents}) == 5

    # 锁必须在 finally 里释放，不能留下过期租约挡住后续请求
    from app.task_engine import runtime_locks
    async with AsyncSessionLocal() as s:
        held = (await s.execute(select(runtime_locks).where(
            runtime_locks.c.lock_key == f"chatgroup:{cid}:marketing"))).all()
    assert held == []


@pytest.mark.asyncio
async def test_ensure_blocked_by_quota_without_creating_agents(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    await _set_owner_quota(t.alice["id"], cid, 2)

    r = await client.post(ENSURE_URL, json={"company_id": cid},
                          headers=t.alice["headers"])
    assert r.status_code == 409, r.text
    assert "配额" in r.json()["detail"]

    # 一个员工都不能超编，组也不能建出来
    assert await _rows(Agent, cid) == []
    assert await _rows(ChatGroup, cid) == []

    reqs = await _rows(ApprovalRequest, cid)
    assert len(reqs) == 1
    assert reqs[0].action_type == "agent_quota"
    assert reqs[0].payload["adding"] == 5
    assert reqs[0].payload["quota"] == 2
    assert reqs[0].payload["overage"] == 3


@pytest.mark.asyncio
async def test_ensure_succeeds_with_exact_quota(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    await _set_owner_quota(t.alice["id"], cid, 5)

    r = await client.post(ENSURE_URL, json={"company_id": cid},
                          headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 5
    assert len(body["group"]["members"]) == 5

    groups = await _rows(ChatGroup, cid)
    agents = await _rows(Agent, cid)
    assert len(groups) == 1
    assert {a.id for a in agents} == set(groups[0].agent_ids)
