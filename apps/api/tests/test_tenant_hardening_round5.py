"""阶段 0 安全止血：performance / reflection 的跨租户越权回归。

v5.14.0 审查确认了两处真实越权（已人工复核，不是误报）：

1. `GET /performance/agents/{agent_id}` 只校验登录 → 知道他司员工 id
   就能读到别人的绩效；`POST /performance/company/{id}/snapshot` 与
   `GET .../history` 直接接受任意 `company_id`（前者还会往他人公司写快照）。
2. `POST /reflection/proposals/{pid}/decide` 按 pid 取出提案就审批，
   `apply=true` 时还会把内容写进该提案所属 Agent 所在公司的知识库。
   该 router 虽挂了 `require_company_scope()`，但请求里没有 `company_id`
   时它是**跳过**，所以这个端点从来没被那层门禁覆盖过。

用例全部走真实 ASGI + 真实鉴权（不 mock 依赖）：跨租户一律 404
（与"不存在"不可区分，避免用 id 枚举），本公司 owner 必须成功——
没有这条正向对照，"404"可能只是因为接口被改坏了。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select


@pytest.fixture
async def two_tenants(client, tenants) -> SimpleNamespace:
    """两家公司各创造一个 Agent，全部走真实接口。"""
    made = {}
    for label in ("alice", "bob"):
        user = getattr(tenants, label)
        co = getattr(tenants, f"{label}_co")
        r = await client.post("/agents", json={
            "company_id": co["id"], "name": f"{label} 的员工", "role": "测试"},
            headers=user["headers"])
        assert r.status_code == 201, f"建 Agent 失败: {r.status_code} {r.text}"
        made[label] = r.json()
    return SimpleNamespace(tenants=tenants,
                           alice_agent=made["alice"], bob_agent=made["bob"])


async def _make_proposal(agent_id: str, company_id: str, *,
                         proposal_type: str = "knowledge_update",
                         payload: dict | None = None) -> str:
    """直接落库造一条 PROPOSED 提案。

    走复盘链路需要真实模型（record_experience → reflect），而这里要测的是
    审批端的租户门禁，所以只补最小的事实链：经历 → 复盘 → 提案。
    """
    from app.db.session import AsyncSessionLocal
    from app.models.reflection import (AgentExperience, AgentReflection,
                                       ImprovementProposal)

    exp_id, refl_id, pid = (str(uuid.uuid4()) for _ in range(3))
    async with AsyncSessionLocal() as db:
        db.add(AgentExperience(id=exp_id, agent_id=agent_id,
                               company_id=company_id,
                               title="一次工作经历", outcome="SUCCESS"))
        db.add(AgentReflection(id=refl_id, experience_id=exp_id,
                               agent_id=agent_id, company_id=company_id))
        db.add(ImprovementProposal(
            id=pid, reflection_id=refl_id, agent_id=agent_id,
            title="改进提案", description="把这条经验写进知识库",
            proposal_type=proposal_type,
            payload=payload or {"content": "经验正文"},
            status="PROPOSED"))
        await db.commit()
    return pid


async def _proposal_state(pid: str) -> tuple[str, str | None]:
    from app.db.session import AsyncSessionLocal
    from app.models.reflection import ImprovementProposal

    async with AsyncSessionLocal() as db:
        row = await db.get(ImprovementProposal, pid)
        assert row is not None, "提案被意外删除"
        return row.status, row.decided_by


async def _snapshot_count(company_id: str) -> int:
    from app.db.session import AsyncSessionLocal
    from app.models.performance import PerformanceSnapshot

    async with AsyncSessionLocal() as db:
        return int((await db.execute(
            select(func.count()).select_from(PerformanceSnapshot).where(
                PerformanceSnapshot.company_id == company_id))).scalar() or 0)


async def _kb_doc_companies() -> list[str]:
    from app.db.session import AsyncSessionLocal
    from app.models.knowledge import KBDocument

    async with AsyncSessionLocal() as db:
        return [d.company_id for d in
                (await db.execute(select(KBDocument))).scalars().all()]


# ---------------------------------------------------------------- 绩效

async def test_agent_performance_rejects_foreign_agent(client, two_tenants):
    t = two_tenants.tenants
    r = await client.get(f"/performance/agents/{two_tenants.bob_agent['id']}",
                         headers=t.alice["headers"])
    assert r.status_code == 404, \
        f"他司员工绩效可读：{r.status_code} {r.text[:160]}"

    # 不存在的 id 也是 404：越权与不存在不可区分，不能用差异状态码枚举
    r = await client.get(f"/performance/agents/{uuid.uuid4()}",
                         headers=t.alice["headers"])
    assert r.status_code == 404


async def test_snapshot_write_rejects_foreign_company(client, two_tenants):
    t = two_tenants.tenants
    r = await client.post(f"/performance/company/{t.bob_co['id']}/snapshot",
                          headers=t.alice["headers"])
    assert r.status_code == 404, \
        f"可对他司公司做绩效快照：{r.status_code} {r.text[:160]}"
    assert await _snapshot_count(t.bob_co["id"]) == 0, "越权快照仍被写入了"


async def test_snapshot_history_rejects_foreign_company(client, two_tenants):
    t = two_tenants.tenants
    # 先由属主造一条快照，确保 404 不是因为"本来就没数据"
    r = await client.post(f"/performance/company/{t.bob_co['id']}/snapshot",
                          headers=t.bob["headers"])
    assert r.status_code == 200, r.text
    assert await _snapshot_count(t.bob_co["id"]) == 1

    r = await client.get(f"/performance/company/{t.bob_co['id']}/history",
                         headers=t.alice["headers"])
    assert r.status_code == 404, \
        f"可读他司绩效快照历史：{r.status_code} {r.text[:160]}"
    assert t.alice_co["id"] not in r.text


async def test_owner_can_use_performance_endpoints(client, two_tenants):
    """正向对照：本公司 owner 三个端点都必须可用。"""
    t = two_tenants.tenants
    aid = two_tenants.alice_agent["id"]

    r = await client.get(f"/performance/agents/{aid}", headers=t.alice["headers"])
    assert r.status_code == 200, f"自己员工的绩效读不到：{r.text[:160]}"
    assert r.json()["agent_id"] == aid and "score" in r.json()

    r = await client.post(f"/performance/company/{t.alice_co['id']}/snapshot",
                          headers=t.alice["headers"])
    assert r.status_code == 200, f"自己公司做不了快照：{r.text[:160]}"
    assert [row["agent_name"] for row in r.json()["ranked"]] == ["alice 的员工"]

    r = await client.get(f"/performance/company/{t.alice_co['id']}/history",
                         headers=t.alice["headers"])
    assert r.status_code == 200, f"自己公司的历史读不到：{r.text[:160]}"
    assert [row["agent_id"] for row in r.json()] == [aid]


# ---------------------------------------------------------------- 复盘审批

async def test_decide_rejects_foreign_proposal(client, two_tenants):
    """A 审批 B 的提案：404，且提案状态与知识库都不能被动过。"""
    t = two_tenants.tenants
    pid = await _make_proposal(two_tenants.bob_agent["id"], t.bob_co["id"])

    for decision in ("approve", "reject"):
        r = await client.post(f"/reflection/proposals/{pid}/decide",
                              json={"decision": decision, "apply": True},
                              headers=t.alice["headers"])
        assert r.status_code == 404, \
            f"可审批他司提案（{decision}）：{r.status_code} {r.text[:160]}"

    status, decided_by = await _proposal_state(pid)
    assert status == "PROPOSED", "越权审批改掉了提案状态"
    assert decided_by is None, "越权审批留下了审批人"
    assert await _kb_doc_companies() == [], "apply=true 越权写入了知识库"


async def test_decide_missing_proposal_is_404(client, tenants):
    r = await client.post(f"/reflection/proposals/{uuid.uuid4()}/decide",
                          json={"decision": "approve"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_owner_can_approve_and_apply_own_proposal(client, two_tenants):
    """正向对照；同时钉住 apply=true 的写入落在提案所属公司。"""
    t = two_tenants.tenants
    pid = await _make_proposal(two_tenants.alice_agent["id"], t.alice_co["id"])

    r = await client.post(f"/reflection/proposals/{pid}/decide",
                          json={"decision": "approve", "apply": True},
                          headers=t.alice["headers"])
    assert r.status_code == 200, f"属主审批不了自己的提案：{r.text[:160]}"
    body = r.json()
    assert body == {"id": pid, "status": "APPROVED", "applied": True}, body

    status, decided_by = await _proposal_state(pid)
    assert status == "APPROVED" and decided_by == t.alice["id"]
    assert await _kb_doc_companies() == [t.alice_co["id"]], \
        "apply 写入未限定在提案所属公司"
