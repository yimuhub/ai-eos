"""第五轮修复的护栏：跨租户写入口 + refresh token 复用 + 枚举校验。

这一批的共同点：路径里没有 company_id（router 级 require_company_scope
会直接跳过），或者 ID 只存在于请求体里——只能靠端点自己比对归属。
修复前，任何一个登录用户拿到对方 id 就能改对方的数据。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _agent_of(client, tenant, co, name: str) -> str:
    r = await client.post("/agents", json={"company_id": co["id"], "name": name},
                          headers=tenant["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_learning_profile_rejects_foreign_agent(client, tenants):
    bob_agent = await _agent_of(client, tenants.bob, tenants.bob_co, "Bob 的员工")

    r = await client.get(f"/learning/agents/{bob_agent}/profile",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户读取成长档案未被拦截：{r.status_code} {r.text}"


async def test_review_rejects_foreign_agent(client, tenants):
    bob_agent = await _agent_of(client, tenants.bob, tenants.bob_co, "Bob 的骨干")

    r = await client.post(f"/reviews/agents/{bob_agent}",
                          json={"source": "USER", "score": 1.0,
                                "comment": "恶意打低分"},
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户写考核未被拦截：{r.status_code} {r.text}"

    r = await client.post(f"/reviews/agents/{bob_agent}/auto-review",
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户触发自动考核未被拦截：{r.status_code} {r.text}"


async def test_governance_rollback_rejects_foreign_agent(client, tenants):
    bob_agent = await _agent_of(client, tenants.bob, tenants.bob_co, "Bob 的负责人")

    r = await client.post(f"/governance/agents/{bob_agent}/rollback/1",
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户回滚员工未被拦截：{r.status_code} {r.text}"


async def test_improvement_proposal_requires_matching_agent(client, tenants):
    """自己的 company_id + 别人的 agent_id：提案会一路部署到别家员工身上。"""
    bob_agent = await _agent_of(client, tenants.bob, tenants.bob_co, "Bob 的实验体")

    r = await client.post(
        f"/governance/improvements?company_id={tenants.alice_co['id']}",
        json={"agent_id": bob_agent, "what_improved": "接管提示词"},
        headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户提案未被拦截：{r.status_code} {r.text}"


async def test_skill_scope_rejects_foreign_skill(client, tenants):
    r = await client.post(
        f"/skills/custom?company_id={tenants.bob_co['id']}",
        json={"name": "Bob 的私有技能", "scope": "exclusive"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # 拿自己的 company_id 改别家的技能
    r = await client.patch(
        f"/skills/{sid}/scope?company_id={tenants.alice_co['id']}&scope=public",
        headers=tenants.alice["headers"])
    assert r.status_code in (404, 409), f"跨租户改技能可见性未被拦截：{r.status_code} {r.text}"


async def test_skill_market_preview_requires_login(client):
    """这个端点是"服务端替人出站抓取"，匿名可用等于开放代理。"""
    r = await client.post("/skills/market/fetch-preview",
                          json={"raw_url": "https://raw.githubusercontent.com/x/y/main/SKILL.md"})
    assert r.status_code == 401, f"匿名访问抓取端点未被拦截：{r.status_code} {r.text}"


async def test_task_priority_rejects_unknown_value(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "优先级校验", "priority": "Urgent!"},
        headers=user["headers"])
    assert r.status_code == 400, f"非法优先级未被拦截：{r.status_code} {r.text}"

    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "正常任务"}, headers=user["headers"])
    tid = r.json()["id"]
    r = await client.patch(f"/tasks/{tid}", json={"priority": "not-a-priority"},
                           headers=user["headers"])
    assert r.status_code == 400, f"PATCH 非法优先级未被拦截：{r.status_code} {r.text}"


async def test_project_chat_rejects_foreign_agent(client, tenants):
    r = await client.post(
        f"/projects?company_id={tenants.alice_co['id']}",
        json={"name": "Alice 的项目"}, headers=tenants.alice["headers"])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    bob_agent = await _agent_of(client, tenants.bob, tenants.bob_co, "Bob 的员工")

    r = await client.post(f"/projects/{pid}/chat-operation",
                          json={"message": "让 Bob 的员工来做",
                                "agent_ids": [bob_agent]},
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户拉人进项目未被拦截：{r.status_code} {r.text}"


async def test_refresh_token_cannot_be_reused(client, make_user):
    """并发/重放同一 refresh token：第二次必须失败（CAS 抢占吊销权）。"""
    user = await make_user()
    r = await client.post("/auth/login", json={
        "identity": user["username"], "password": user["password"]})
    assert r.status_code == 200, r.text
    refresh = r.json()["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200, first.text

    second = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert second.status_code == 401, (
        f"同一个 refresh_token 被重复使用成功：{second.status_code} {second.text}")
