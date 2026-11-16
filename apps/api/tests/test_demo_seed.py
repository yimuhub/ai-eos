"""演示公司种子：一键预置、幂等、归属隔离。

覆盖三件事：
1. 未登录一律 401（种子是写操作，不能匿名触发）；
2. 全新用户首次种子：公司 / 2 部门 / 4 员工 / 2 交付单（1 IN_REVIEW 1 APPROVED）
   / 1 个 TODO 任务一次建齐，且**不绑任何模型**（演示公司无需密钥即可浏览）；
3. 幂等与租户隔离：同一用户重复种子不翻倍；用户 B 看不到用户 A 的演示公司。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentModelConfig, AgentStatus
from app.models.artifact import Artifact, ArtifactVersion
from app.models.company import Company, Department
from app.models.task import Task, TaskStatus

pytestmark = pytest.mark.asyncio

DEMO_COMPANY_NAME = "示例公司（演示）"


async def _load_demo_data(company_id: str) -> SimpleNamespace:
    """把该演示公司名下的全部种子行一次查齐，供各用例断言。"""
    async with AsyncSessionLocal() as s:
        company = await s.get(Company, company_id)
        departments = (await s.execute(
            select(Department).where(Department.company_id == company_id)
            .order_by(Department.created_at))).scalars().all()
        agents = (await s.execute(
            select(Agent).where(Agent.company_id == company_id))).scalars().all()
        artifacts = (await s.execute(
            select(Artifact).where(Artifact.company_id == company_id))).scalars().all()
        versions = (await s.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id.in_([a.id for a in artifacts]))
            .order_by(ArtifactVersion.artifact_id))).scalars().all()
        tasks = (await s.execute(
            select(Task).where(Task.company_id == company_id))).scalars().all()
        # 模型绑定按「该公司全部员工」统计：一条都不允许存在
        bindings = (await s.execute(
            select(AgentModelConfig).join(
                Agent, Agent.id == AgentModelConfig.agent_id)
            .where(Agent.company_id == company_id))).scalars().all()
    return SimpleNamespace(company=company, departments=departments,
                           agents=agents, artifacts=artifacts,
                           versions=versions, tasks=tasks, bindings=bindings)


async def _count_companies_owned(user_id: str) -> int:
    async with AsyncSessionLocal() as s:
        return int((await s.execute(
            select(func.count(Company.id)).where(
                Company.owner_id == user_id))).scalar_one())


async def _seed(client, user: dict) -> dict:
    r = await client.post("/demo/seed", headers=user["headers"])
    assert r.status_code == 200, f"种子失败: {r.status_code} {r.text}"
    return r.json()


def _agent_by_name(agents, keyword: str) -> Agent:
    return next(a for a in agents if keyword in a.name)


async def test_seed_requires_auth(client):
    """未登录（不带 token）不能触发种子。"""
    r = await client.post("/demo/seed")
    assert r.status_code == 401, r.text


async def test_seed_fresh_user_creates_full_demo_company(client, make_user):
    """全新用户首次种子：公司/部门/员工/交付/任务一次建齐，且零模型绑定。"""
    user = await make_user()
    body = await _seed(client, user)

    assert body["already_seeded"] is False
    assert body["company_name"] == DEMO_COMPANY_NAME
    assert body["agents"] == 4
    assert body["deliverables"] == 2
    assert body["task_id"]

    data = await _load_demo_data(body["company_id"])

    # 公司：owner 即当前用户，description 说明这是预置演示数据
    assert data.company is not None
    assert data.company.owner_id == user["id"]
    assert data.company.name == DEMO_COMPANY_NAME
    assert data.company.description

    # 部门：创意部 / 运营部
    assert {d.name for d in data.departments} == {"创意部", "运营部"}

    # 员工：4 名，全部 IDLE；陈墨是 leader（无上级），其余都汇报给他
    assert len(data.agents) == 4
    assert all(a.status == AgentStatus.IDLE for a in data.agents)
    assert all(a.system_prompt for a in data.agents)  # 100 字左右中文人设
    leader = _agent_by_name(data.agents, "陈墨")
    assert leader.manager_agent_id is None
    assert leader.department_id == _agent_by_name(data.departments, "创意部").id
    for a in data.agents:
        if a.id != leader.id:
            assert a.manager_agent_id == leader.id
    assert len({a.id for a in data.agents}) == 4  # id 互不重复

    # 模型绑定：演示公司不需要密钥即可浏览 → 该公司下 AgentModelConfig 为 0
    assert len(data.bindings) == 0

    # 交付单：2 份，1 IN_REVIEW + 1 APPROVED，均为公司级（project_id=None）
    assert len(data.artifacts) == 2
    statuses = sorted(a.status.value for a in data.artifacts)
    assert statuses == ["APPROVED", "IN_REVIEW"]
    assert all(a.project_id is None for a in data.artifacts)
    assert all(a.created_by == user["id"] for a in data.artifacts)
    assert all(a.current_version == 1 for a in data.artifacts)

    # 版本：每份交付单恰好 1 个 v1，变更说明与提交人都按演示设定
    assert len(data.versions) == 2
    assert all(v.version == 1 for v in data.versions)
    assert all(v.change_note == "演示初始版本" for v in data.versions)
    lin = _agent_by_name(data.agents, "林小满")
    xhs = next(a for a in data.artifacts if "种草文案" in a.name)
    v_xhs = next(v for v in data.versions if v.artifact_id == xhs.id)
    assert v_xhs.submitted_by == lin.id
    # 文案体：钩子 + 3 个种草点 + 行动号召 + 话题标签，300~500 字
    assert 300 <= len(v_xhs.content) <= 500
    assert "种草点 3" in v_xhs.content
    assert "#" in v_xhs.content
    # 周报体：数据占位符 + 审核痕迹
    report = next(a for a in data.artifacts if "周报" in a.name)
    v_report = next(v for v in data.versions if v.artifact_id == report.id)
    assert "互动 1.2w" in v_report.content
    assert v_report.reviewed_by == leader.id

    # 任务：1 个 TODO，指派给文案策划，不自动开工
    assert len(data.tasks) == 1
    t = data.tasks[0]
    assert t.title == "写一篇国庆出游的小红书种草文案"
    assert t.status == TaskStatus.TODO
    assert t.auto_start is False
    assert t.assigned_agent_id == lin.id
    assert t.created_by == user["id"]
    assert t.company_id == body["company_id"]
    assert body["task_id"] == t.id


async def test_seed_is_idempotent(client, make_user):
    """同一用户二次种子 → already_seeded=true，任何行都不翻倍。"""
    user = await make_user()
    first = await _seed(client, user)
    second = await _seed(client, user)

    assert second["already_seeded"] is True
    assert second["company_id"] == first["company_id"]
    assert second["company_name"] == DEMO_COMPANY_NAME
    assert "task_id" not in second  # 幂等命中只回公司三件套

    data = await _load_demo_data(first["company_id"])
    assert len(data.departments) == 2
    assert len(data.agents) == 4
    assert len(data.artifacts) == 2
    assert len(data.versions) == 2
    assert len(data.tasks) == 1
    assert len(data.bindings) == 0
    # 公司也只此一家，没有为重复请求另建新行
    assert await _count_companies_owned(user["id"]) == 1


async def test_demo_company_isolated_per_user(client, make_user):
    """用户 B 看不到用户 A 的演示公司：GET /companies 只返回自己的。"""
    alice = await make_user()
    bob = await make_user()
    await _seed(client, alice)

    r = await client.get("/companies", headers=bob["headers"])
    assert r.status_code == 200, r.text
    bob_companies = r.json()
    assert all(c["owner_id"] == bob["id"] for c in bob_companies)
    assert all(c["name"] != DEMO_COMPANY_NAME for c in bob_companies)

    # A 自己能看到演示公司；B 再种得到的是自己的另一家（互不干扰）
    r_alice = await client.get("/companies", headers=alice["headers"])
    assert r_alice.status_code == 200, r_alice.text
    alice_companies = r_alice.json()
    assert any(c["name"] == DEMO_COMPANY_NAME for c in alice_companies)

    bob_body = await _seed(client, bob)
    assert bob_body["already_seeded"] is False
    assert bob_body["company_id"] != ""
    data = await _load_demo_data(bob_body["company_id"])
    assert data.company.owner_id == bob["id"]
    assert len(data.agents) == 4
