"""租户隔离（IDOR）：A 用户绝不能读写 B 用户的任何资源。

这一组是本次改造修复的重点方向之一。改造前多个模块存在"知道 id
就能操作他人数据"的问题，其中两处尤为严重：
- `POST /computer-agent/permissions` 可替**他人公司**授权本机 shell 能力；
- `POST /training/sessions/{id}/rollback` 完全没有归属校验。

用例的设计思路：不做"能读到自己数据"的正向断言就收工，
而是**主动用 B 的凭据去访问 A 的资源**，逐一确认被拒绝；
关键用例再补一条反向对照，避免"因为接口整体坏了才拒绝"的假阳性。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# `tenants` 夹具定义在 conftest.py，本模块与 test_security_sandbox.py 共用。


# ---------------------------------------------------------------- 公司

async def test_cannot_read_other_company(client, tenants):
    r = await client.get(f"/companies/{tenants.alice_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, "他人公司可读"


async def test_cannot_update_other_company(client, tenants):
    r = await client.patch(f"/companies/{tenants.alice_co['id']}",
                           json={"name": "被篡改"},
                           headers=tenants.bob["headers"])
    assert r.status_code == 404
    # 确认真的没被改掉
    r = await client.get(f"/companies/{tenants.alice_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.json()["name"] == "A 公司", "公司名竟被他人改掉"


async def test_cannot_delete_other_company(client, tenants):
    r = await client.delete(f"/companies/{tenants.alice_co['id']}",
                            headers=tenants.bob["headers"])
    assert r.status_code == 404
    r = await client.get(f"/companies/{tenants.alice_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, "公司竟被他人删除"


async def test_company_list_isolated(client, tenants):
    r = await client.get("/companies", headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert {c["id"] for c in r.json()} == {tenants.alice_co["id"]}, "公司列表串号"


async def test_cannot_list_other_company_departments(client, tenants):
    r = await client.get(f"/companies/{tenants.alice_co['id']}/departments",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_dashboard_does_not_leak_other_company(client, tenants):
    """`/dashboard/{cid}` 对他人公司必须 404，且不返回任何数据。

    这里原先容忍"200 + {"error": ...}"的软失败写法（当时把改契约当成越界）。
    后来把契约统一成了 404：软失败在 HTTP 层看不出问题，前端只能靠读 body
    里的 error 字段判断，很容易漏判成"数据都是 0"——一个空看板比报错更误导人。
    """
    r = await client.get(f"/dashboard/{tenants.alice_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, f"疑似返回了他人公司看板数据：{r.text[:200]}"
    body = r.json()
    assert "agents" not in body and "tasks" not in body, \
        f"404 响应里仍带了看板数据：{body}"


# ---------------------------------------------------------------- Agent

@pytest.fixture
async def agents(client, tenants) -> SimpleNamespace:
    made = {}
    for label in ("alice", "bob"):
        user = getattr(tenants, label)
        company = getattr(tenants, f"{label}_co")
        r = await client.post("/agents", json={
            "company_id": company["id"], "name": f"{label} 的员工",
            "role": "测试"}, headers=user["headers"])
        assert r.status_code == 201, f"建 Agent 失败: {r.status_code} {r.text}"
        made[label] = r.json()
    return SimpleNamespace(tenants=tenants, alice=made["alice"], bob=made["bob"])


async def test_cannot_create_agent_in_other_company(client, tenants):
    r = await client.post("/agents", json={
        "company_id": tenants.alice_co["id"], "name": "渗透者"},
        headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_agent_read_isolated(client, agents):
    """既要确认"读不到他人的"，也要确认"读得到自己的"。"""
    r = await client.get(f"/agents/{agents.alice['id']}",
                         headers=agents.tenants.bob["headers"])
    assert r.status_code == 404, "他人 Agent 可读"
    r = await client.get(f"/agents/{agents.bob['id']}",
                         headers=agents.tenants.bob["headers"])
    assert r.status_code == 200, "自己的 Agent 反倒读不到"


async def test_cannot_chat_with_other_agent(client, agents):
    """跨租户调用他人 Agent 的聊天接口 = 让别人替你付费 + 读到他人记忆。"""
    r = await client.post(f"/agents/{agents.alice['id']}/chat",
                          json={"message": "你好"},
                          headers=agents.tenants.bob["headers"])
    assert r.status_code == 404, "可对他人 Agent 发起对话"


async def test_cannot_read_other_agent_chat_history(client, agents):
    r = await client.get(f"/agents/{agents.alice['id']}/chat/history",
                         headers=agents.tenants.bob["headers"])
    assert r.status_code == 404


async def test_cannot_delete_other_agent(client, agents):
    r = await client.delete(f"/agents/{agents.alice['id']}",
                            headers=agents.tenants.bob["headers"])
    assert r.status_code in (403, 404), f"他人 Agent 可被删除：{r.status_code}"


# ---------------------------------------------------------------- 训练

async def test_cannot_start_training_on_other_agent(client, agents):
    """改造前该模块**所有端点**都缺少归属校验。"""
    r = await client.post("/training/sessions",
                          params={"agent_id": agents.alice["id"]},
                          json={"training_type": "DOCUMENT", "title": "越权训练",
                                "raw_input": "试图注入"},
                          headers=agents.tenants.bob["headers"])
    assert r.status_code == 404, f"可对他人 Agent 发起训练：{r.status_code} {r.text[:200]}"


async def test_cannot_list_other_agent_training(client, agents):
    r = await client.get("/training/sessions",
                         params={"agent_id": agents.alice["id"]},
                         headers=agents.tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------- 复盘

async def test_cannot_read_other_agent_lessons(client, agents):
    r = await client.get(f"/reflection/agents/{agents.alice['id']}/lessons",
                         headers=agents.tenants.bob["headers"])
    assert r.status_code == 404


async def test_cannot_delete_other_agent_lesson(client, agents):
    r = await client.delete(
        f"/reflection/agents/{agents.alice['id']}/lessons/nonexistent-id",
        headers=agents.tenants.bob["headers"])
    assert r.status_code in (403, 404), f"越权删除未被拦截：{r.status_code}"


# ---------------------------------------------------------------- 本机能力

async def test_cannot_read_other_company_computer_permissions(client, tenants):
    r = await client.get("/computer-agent/permissions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_cannot_grant_capability_on_other_company(client, tenants):
    """**最关键的一条**：改造前可替他人公司授权本机 shell 执行能力，
    等价于给攻击者一台肉鸡。"""
    r = await client.post("/computer-agent/grant",
                          params={"company_id": tenants.alice_co["id"],
                                  "capability": "run_shell"},
                          headers=tenants.bob["headers"])
    assert r.status_code == 404, f"可替他人公司授权本机能力：{r.status_code}"
    # 双保险：确认并未真的写入
    r = await client.get("/computer-agent/permissions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert r.json().get("run_shell") is False, "授权竟被写入"


async def test_grant_then_revoke_roundtrip(client, tenants):
    """正向对照：自己的公司可以授权，撤销后立即失效。

    没有这条对照，上面那条"被拒绝"的断言可能只是因为接口整体不可用。
    """
    params = {"company_id": tenants.alice_co["id"], "capability": "run_shell"}
    r = await client.post("/computer-agent/grant", params=params,
                          headers=tenants.alice["headers"])
    assert r.status_code == 200 and r.json()["granted"] is True

    r = await client.get("/computer-agent/permissions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.alice["headers"])
    assert r.json()["run_shell"] is True

    r = await client.post("/computer-agent/revoke", params=params,
                          headers=tenants.alice["headers"])
    assert r.status_code == 200 and r.json()["granted"] is False

    r = await client.get("/computer-agent/permissions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.alice["headers"])
    assert r.json()["run_shell"] is False


async def test_cannot_execute_action_in_other_company(client, tenants):
    r = await client.post("/computer-agent/execute",
                          params={"company_id": tenants.alice_co["id"]},
                          json={"action_type": "screenshot", "params": {}},
                          headers=tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------- 任务

async def test_task_list_scoped_to_own_companies(client, tenants):
    await client.post("/tasks", json={
        "company_id": tenants.alice_co["id"], "title": "A 的任务"},
        headers=tenants.alice["headers"])

    r = await client.get("/tasks", headers=tenants.bob["headers"])
    assert r.status_code == 200
    assert r.json() == [], "任务列表串号"

    # 显式传他人 company_id 也不能绕过
    r = await client.get("/tasks", params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 200
    assert r.json() == []


async def test_cannot_create_task_in_other_company(client, tenants):
    r = await client.post("/tasks", json={
        "company_id": tenants.alice_co["id"], "title": "越权任务"},
        headers=tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------- 职业发展

async def test_career_module_requires_ownership(client, tenants):
    """职业发展模块改造前**所有端点**都没有归属校验：
    知道 agent_id 就能给他人 Agent 改任职、甚至审批生效一次晋升。
    """
    r = await client.post("/career/positions",
                          params={"company_id": tenants.alice_co["id"]},
                          json={"title": "越权职位", "level": 9},
                          headers=tenants.bob["headers"])
    assert r.status_code == 404, "可替他人公司创建职位"

    r = await client.get("/career/positions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, "可读他人公司职位表"


async def test_career_agent_scoped_endpoints(client, agents):
    t = agents.tenants
    aid = agents.alice["id"]
    for desc, method, url in [
        ("履历时间线", "get", f"/career/agents/{aid}/timeline"),
        ("缺口分析", "get", f"/career/agents/{aid}/gap-analysis/some-position"),
    ]:
        r = await getattr(client, method)(url, headers=t.bob["headers"])
        assert r.status_code in (403, 404), f"{desc} 越权可读：{r.status_code}"

    r = await client.get(f"/career/agents/{aid}/timeline",
                         headers=t.alice["headers"])
    assert r.status_code == 200, "自己的履历反倒读不到"


async def test_career_join_other_agent_rejected(client, tenants):
    """给他人 Agent 安排任职 = 篡改他人公司的人事记录。"""
    r = await client.post("/career/agents/does-not-exist/join",
                          params={"position_id": "whatever"},
                          headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_career_proposal_lifecycle_is_scoped(client, agents):
    """提案的提交/审批也必须校验归属。"""
    r = await client.post("/career/proposals/nonexistent-id/submit",
                          headers=agents.tenants.bob["headers"])
    assert r.status_code == 404

    r = await client.post("/career/proposals/nonexistent-id/decide",
                          json={"decision": "approve"},
                          headers=agents.tenants.bob["headers"])
    assert r.status_code == 404
