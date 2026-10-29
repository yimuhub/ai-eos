"""跨租户"读"越权的运行时攻击用例（P0-1 收口）。

`test_permissions.py` 的读端点是 **AST 静态扫描**，只能证明"函数体里出现了
校验关键字"，证明不了"校验对象是调用者、而不是调用者自己传进来的 id"。
本文件与 `test_tenancy_attacks.py` 同一套路：真实 HTTP 调用，双租户种子数据，
B 拿 A 的资源 id / A 的 company_id 去打，断言 404（不是 403——403 会告诉
攻击者"这个 id 存在"，可用于枚举租户）。

同时每个用例都断言"A 访问自己的资源 → 200 且数据正确"：只测 404 很容易
写出"把校验加得太狠、功能一起锁死"的假修复。

本轮覆盖：knowledge / factory / tokens 统计 / sandbox / mailboxes / app_alias。
"""
from types import SimpleNamespace
import uuid

import pytest

from app.browser_agent import BrowserActionLog
from app.db.session import AsyncSessionLocal
from app.factory_engine import AgentFactoryJob, FactoryJobStatus
from app.models.expert_template import ExpertTemplate
from app.models.skill_growth import SkillNode
from app.models.token_usage import TokenUsage


async def _make_agent(client, user, company_id: str, name: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name,
        "role": "research", "department": "研究部"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_doc(client, user, company_id: str, title: str,
                    content: str, **extra) -> str:
    r = await client.post(f"/knowledge/documents?company_id={company_id}",
                          json={"title": title, "content": content, **extra},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
async def surface(client, tenants) -> SimpleNamespace:
    """A 公司真实存在的资源——B 的攻击目标；B 也备一份用于对照。"""
    a, b = tenants.alice, tenants.bob
    a_co, b_co = tenants.alice_co["id"], tenants.bob_co["id"]

    a_agent = await _make_agent(client, a, a_co, "A 的员工")
    b_agent = await _make_agent(client, b, b_co, "B 的员工")

    a_doc = await _make_doc(client, a, a_co, "A 采购底价",
                            "采购底价 800 万，仅限 A 公司内部查看")
    b_doc = await _make_doc(client, b, b_co, "B 采购底价",
                            "B 的采购底价是 900 万")

    # 工厂任务：蓝图生成要真实模型，离线环境直接落库种子
    async with AsyncSessionLocal() as db:
        a_job = AgentFactoryJob(
            id=str(__import__("uuid").uuid4()), company_id=a_co,
            goal="A 的工厂目标", blueprint={"name": "A 候选人", "role": "研究"},
            status=FactoryJobStatus.BLUEPRINT, created_by=a["id"])
        b_job = AgentFactoryJob(
            id=str(__import__("uuid").uuid4()), company_id=b_co,
            goal="B 的工厂目标", blueprint={"name": "B 候选人", "role": "研究"},
            status=FactoryJobStatus.BLUEPRINT, created_by=b["id"])
        db.add_all([a_job, b_job])
        # 用量流水：A 15 tokens，B 150 tokens，便于断言"没串数据"
        db.add_all([
            TokenUsage(company_id=a_co, agent_id=a_agent, agent_name="A 员工",
                       scenario="CHAT", input_tokens=10, output_tokens=5,
                       total_tokens=15, total_cost=0.15),
            TokenUsage(company_id=b_co, agent_id=b_agent, agent_name="B 员工",
                       scenario="CHAT", input_tokens=100, output_tokens=50,
                       total_tokens=150, total_cost=1.5),
        ])
        await db.commit()
        a_job_id, b_job_id = a_job.id, b_job.id

    # 沙箱：A 的工作区 + 运行中会话
    r = await client.post("/sandbox/workspaces",
                          params={"name": "A 工作区", "company_id": a_co},
                          headers=a["headers"])
    assert r.status_code == 201, r.text
    a_wid = r.json()["id"]
    r = await client.post(f"/sandbox/workspaces/{a_wid}/sessions",
                          json={"goal": "整理资料"}, headers=a["headers"])
    assert r.status_code == 201, r.text
    a_sid = r.json()["id"]

    # 邮箱：A 的员工邮箱 + 一封来信
    r = await client.post("/mailboxes", json={
        "agent_id": a_agent,
        "alias": f"a-{__import__('uuid').uuid4().hex[:8]}@aieos.local"},
        headers=a["headers"])
    assert r.status_code == 200, r.text
    a_mbid = r.json()["id"]
    r = await client.post("/mailboxes/messages", json={
        "mailbox_id": a_mbid, "subject": "A 的机密来信", "body": "正文",
        "from_addr": "client@example.com"}, headers=a["headers"])
    assert r.status_code == 200, r.text
    a_msg = r.json()["id"]

    # 应用别名：拿 A 本机已装应用之一做路径覆盖（无已装应用时退化为 Finder）
    r = await client.get(f"/app-alias/installed?company_id={a_co}",
                         headers=a["headers"])
    assert r.status_code == 200, r.text
    installed = r.json().get("installed") or []
    alias_target = installed[0]["name"] if installed else "Finder"
    r = await client.post(f"/app-alias/rules?company_id={a_co}", json={
        "alias": f"打开{__import__('uuid').uuid4().hex[:4]}",
        "app_name": alias_target, "app_path": "/tmp/a-only-app"},
        headers=a["headers"])
    assert r.status_code == 200, r.text

    return SimpleNamespace(
        a=a, b=b, a_co=a_co, b_co=b_co,
        a_agent=a_agent, b_agent=b_agent, a_doc=a_doc, b_doc=b_doc,
        a_job=a_job_id, b_job=b_job_id,
        a_wid=a_wid, a_sid=a_sid, a_mbid=a_mbid, a_msg=a_msg,
        alias_target=alias_target)


# ---------------------------------------------------------------------------
# knowledge：创建 / 列表 / 详情 / 检索 / RAG
# ---------------------------------------------------------------------------

async def test_knowledge_reads_are_tenant_scoped(client, surface):
    """B 拿 A 的 company_id / doc_id 访问知识库必须 404，A 自己必须照常可用。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.get(f"/knowledge/documents?company_id={surface.a_co}",
                         headers=b)
    assert r.status_code == 404, "B 列出了 A 公司的知识库文档（跨租户读取）"

    r = await client.get(f"/knowledge/documents/{surface.a_doc}", headers=b)
    assert r.status_code == 404, "B 按文档 id 读到了 A 的正文"

    r = await client.get(
        f"/knowledge/search?company_id={surface.a_co}&q=采购底价", headers=b)
    assert r.status_code == 404, "B 检索了 A 的知识库"

    r = await client.get(
        f"/knowledge/rag?company_id={surface.a_co}&q=采购底价", headers=b)
    assert r.status_code == 404, "B 通过 RAG 把 A 的正文拼进了上下文"

    r = await client.post(f"/knowledge/documents?company_id={surface.a_co}",
                          json={"title": "投毒", "content": "x"}, headers=b)
    assert r.status_code == 404, "B 往 A 的知识库写了文档"

    # 属主必须仍然可用，否则就是"用不可用换安全"
    r = await client.get(f"/knowledge/documents?company_id={surface.a_co}",
                         headers=a)
    assert r.status_code == 200, f"A 列不出自己的文档：{r.text[:160]}"
    assert [d["id"] for d in r.json()] == [surface.a_doc], \
        "A 的文档列表内容不对（多出别人的或漏了自己的）"

    r = await client.get(f"/knowledge/documents/{surface.a_doc}", headers=a)
    assert r.status_code == 200, f"A 读不到自己的文档：{r.text[:160]}"
    assert "采购底价" in r.json()["content"], "A 读到的正文不对"

    r = await client.get(
        f"/knowledge/search?company_id={surface.a_co}&q=采购底价", headers=a)
    assert r.status_code == 200 and r.json()["results"], "A 检索不到自己的知识库"

    r = await client.get(
        f"/knowledge/rag?company_id={surface.a_co}&q=采购底价", headers=a)
    assert r.status_code == 200 and "采购底价" in r.json()["context"], \
        "A 的 RAG 上下文里没有自己的正文"


async def test_knowledge_private_scope_is_agent_scoped(client, surface):
    """scope=private 的文档必须同时校验 company_id 与 agent_id 归属。"""
    a, b = surface.a["headers"], surface.b["headers"]

    # B 用 A 的公司 + A 的员工建私人文档：公司校验先拦下
    r = await client.post(
        f"/knowledge/documents?company_id={surface.a_co}",
        json={"title": "偷看", "content": "x", "scope": "private",
              "agent_id": surface.a_agent}, headers=b)
    assert r.status_code == 404, "B 用 A 的公司与员工建了私人文档"

    # A 用自家公司 + B 的员工建私人文档：agent 校验必须拦下
    r = await client.post(
        f"/knowledge/documents?company_id={surface.a_co}",
        json={"title": "挂靠", "content": "x", "scope": "private",
              "agent_id": surface.b_agent}, headers=a)
    assert r.status_code == 404, "A 可以把他人员工设为自家私人库的属主"

    # 合法路径仍然可用
    r = await client.post(
        f"/knowledge/documents?company_id={surface.a_co}",
        json={"title": "A 的私人笔记", "content": "仅 A 的员工可见",
              "scope": "private", "agent_id": surface.a_agent}, headers=a)
    assert r.status_code == 200, f"A 建不了自家的私人文档：{r.text[:160]}"
    doc_id = r.json()["id"]

    r = await client.get(f"/knowledge/documents/{doc_id}", headers=a)
    assert r.status_code == 200, "A 读不到自家的私人文档"
    r = await client.get(f"/knowledge/documents/{doc_id}", headers=b)
    assert r.status_code == 404, "B 读到了 A 的私人文档"


# ---------------------------------------------------------------------------
# factory：6 个按 job_id 的端点
# ---------------------------------------------------------------------------

async def test_factory_jobs_are_tenant_scoped(client, surface):
    """B 拿 A 的 job_id 做任何操作都必须 404；A 自己不能被误伤。"""
    a, b = surface.a["headers"], surface.b["headers"]
    job = surface.a_job

    r = await client.get(f"/factory/jobs?company_id={surface.a_co}", headers=b)
    assert r.status_code == 404, "B 列出了 A 公司的工厂任务"

    cases = [
        ("GET", f"/factory/jobs/{job}/training-detail", None),
        ("POST", f"/factory/jobs/{job}/test-result?passed=true", None),
        ("POST", f"/factory/jobs/{job}/review-training", {"approve": True}),
        ("POST", f"/factory/jobs/{job}/approve", {}),
        ("POST", f"/factory/jobs/{job}/ask", {"question": "你是谁"}),
    ]
    for method, path, body in cases:
        if method == "GET":
            r = await client.get(path, headers=b)
        else:
            r = await client.post(path, json=body, headers=b)
        assert r.status_code == 404, \
            f"{path} 允许 B 操作 A 的工厂任务：{r.status_code} {r.text[:120]}"

    # 属主可用且数据正确
    r = await client.get(f"/factory/jobs?company_id={surface.a_co}", headers=a)
    assert r.status_code == 200, f"A 列不出自己的工厂任务：{r.text[:160]}"
    assert [j["id"] for j in r.json()] == [surface.a_job], \
        "A 的任务列表混进了 B 的任务或漏了自己的"

    r = await client.get(f"/factory/jobs/{job}/training-detail", headers=a)
    assert r.status_code == 200 and r.json()["job_id"] == surface.a_job, \
        "A 读不到自己的培训验收详情"

    # 状态机拒绝（409）说明归属校验已通过、不是把 A 也挡成 404
    r = await client.post(f"/factory/jobs/{job}/test-result?passed=true",
                          headers=a)
    assert r.status_code == 409, \
        f"A 被误判为无权操作自己的任务：{r.status_code} {r.text[:120]}"


# ---------------------------------------------------------------------------
# tokens：统计端点 + 计费单价
# ---------------------------------------------------------------------------

async def test_token_stats_are_tenant_scoped(client, surface):
    """不传 company_id 时按自有公司聚合；传他人 company_id 一律 404。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.get("/tokens/stats/summary", headers=b)
    assert r.status_code == 200, r.text
    assert r.json()["total_tokens"] == 150, \
        f"B 的总览里混入了 A 的用量：{r.json()['total_tokens']}"

    r = await client.get("/tokens/stats/summary", headers=a)
    assert r.status_code == 200 and r.json()["total_tokens"] == 15, \
        "A 的总览数字不对（应为自己的 15 tokens）"

    r = await client.get(
        f"/tokens/stats/summary?company_id={surface.a_co}", headers=b)
    assert r.status_code == 404, "B 指定 A 公司仍能读用量汇总"

    for path in ("/tokens/stats/by-agent", "/tokens/stats/by-department",
                 "/tokens/stats/by-project", "/tokens/stats/by-task",
                 "/tokens/stats/by-scenario", "/tokens/usage-logs"):
        r = await client.get(f"{path}?company_id={surface.a_co}", headers=b)
        assert r.status_code == 404, \
            f"{path} 允许 B 按 A 的 company_id 读取用量：{r.status_code}"

    # 公司维度不能枚举全平台
    r = await client.get("/tokens/stats/by-company", headers=b)
    keys = {row["key"] for row in r.json()}
    assert keys == {surface.b_co}, f"B 的公司维度看到了其他租户：{keys}"

    r = await client.get("/tokens/stats/by-company", headers=a)
    keys = {row["key"] for row in r.json()}
    assert keys == {surface.a_co}, f"A 的公司维度数据不对：{keys}"

    # 属主可用
    r = await client.get(f"/tokens/stats/by-agent?company_id={surface.a_co}",
                         headers=a)
    assert r.status_code == 200 and any(
        row["name"] == "A 的员工" for row in r.json()), \
        f"A 看不到自己的按 Agent 用量：{r.json()}"


async def test_token_pricing_cannot_be_hijacked(client, surface):
    """POST /tokens/pricing 按 model_id 幂等，必须校验模型归属。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.post("/models", json={
        "name": "A 的模型", "provider_type": "openai_compat",
        "base_url": "http://127.0.0.1:9/v1", "api_key_env": "NOT_A_REAL_KEY_ENV",
        "model_id": "a-model", "company_id": surface.a_co}, headers=a)
    assert r.status_code == 201, r.text
    a_model = r.json()["id"]

    r = await client.post("/tokens/pricing", json={
        "model_id": a_model, "billing_mode": "USAGE",
        "input_price_per_m": 0.01}, headers=b)
    assert r.status_code == 404, "B 覆盖了 A 公司模型的计费单价"

    r = await client.post("/tokens/pricing", json={
        "model_id": a_model, "billing_mode": "USAGE",
        "input_price_per_m": 0.02}, headers=a)
    assert r.status_code == 201, f"A 配不了自己模型的单价：{r.text[:160]}"

    r = await client.get("/tokens/pricing", headers=a)
    row = next(p for p in r.json() if p["model_id"] == a_model)
    assert row["input_price_per_m"] == 0.02, "A 的单价被越权请求污染"


# ---------------------------------------------------------------------------
# sandbox：工作区 / 会话 / 动作
# ---------------------------------------------------------------------------

async def test_sandbox_workspace_session_action_scoped(client, surface):
    """wid / sid / aid 三个维度都不能跨租户。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.get(f"/sandbox/workspaces?company_id={surface.a_co}",
                         headers=b)
    assert r.status_code == 404, "B 列出了 A 公司的沙箱工作区"

    r = await client.post(f"/sandbox/workspaces/{surface.a_wid}/sessions",
                          json={"goal": "借用"}, headers=b)
    assert r.status_code == 404, "B 在 A 的工作区里开了会话"

    r = await client.get(f"/sandbox/workspaces/{surface.a_wid}/files", headers=b)
    assert r.status_code == 404, "B 读了 A 工作区的文件"

    r = await client.post(f"/sandbox/sessions/{surface.a_sid}/pause", headers=b)
    assert r.status_code == 404, "B 暂停了 A 的会话"

    r = await client.post(f"/sandbox/sessions/{surface.a_sid}/act",
                          json={"action_type": "click"}, headers=b)
    assert r.status_code == 404, "B 在 A 的会话里执行了动作"

    r = await client.get(f"/sandbox/sessions/{surface.a_sid}/actions", headers=b)
    assert r.status_code == 404, "B 读了 A 会话的动作记录"

    # 属主可用 + 高风险动作审批同样按租户隔离
    r = await client.post(f"/sandbox/sessions/{surface.a_sid}/act",
                          json={"action_type": "run_shell",
                                "params": {"cmd": "ls"}}, headers=a)
    assert r.status_code == 200 and r.json().get("requires_approval") is True, \
        f"A 无法提交自己的高风险动作：{r.text[:160]}"
    aid = r.json()["action_id"]

    r = await client.post(f"/sandbox/actions/{aid}/approve", headers=b)
    assert r.status_code == 404, "B 批准了 A 的高风险动作"
    r = await client.post(f"/sandbox/actions/{aid}/approve", headers=a)
    assert r.status_code == 200, f"A 批不了自己的动作：{r.text[:160]}"

    r = await client.get(f"/sandbox/workspaces?company_id={surface.a_co}",
                         headers=a)
    assert r.status_code == 200 and [w["id"] for w in r.json()] == [surface.a_wid], \
        "A 的工作区列表数据不对"
    r = await client.get(f"/sandbox/sessions/{surface.a_sid}/actions", headers=a)
    assert r.status_code == 200 and len(r.json()) == 1, \
        "A 看不到自己会话里的动作记录"


# ---------------------------------------------------------------------------
# mailboxes：邮箱列表 / 邮件列表 / 回复 / 已读
# ---------------------------------------------------------------------------

async def test_mailboxes_are_tenant_scoped(client, surface):
    """邮箱与邮件没有可直接用的 company_id，必须经 Agent 反查归属。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.post("/mailboxes", json={
        "agent_id": surface.a_agent,
        "alias": f"b-{__import__('uuid').uuid4().hex[:8]}@aieos.local"},
        headers=b)
    assert r.status_code == 404, "B 能给 A 的员工开邮箱"

    r = await client.get("/mailboxes", headers=b)
    assert r.status_code == 200 and surface.a_mbid not in [
        m["id"] for m in r.json()], "B 的邮箱列表里出现了 A 的邮箱"

    r = await client.get("/mailboxes/messages", headers=b)
    assert r.status_code == 200 and surface.a_msg not in [
        m["id"] for m in r.json()], "B 的收件箱里出现了 A 的邮件"

    r = await client.get(
        f"/mailboxes/messages?mailbox_id={surface.a_mbid}", headers=b)
    assert r.status_code == 404, "B 按 mailbox_id 读了 A 的邮件"

    r = await client.post(f"/mailboxes/messages/{surface.a_msg}/reply",
                          json={"body": "越权回复"}, headers=b)
    assert r.status_code == 404, "B 以 A 的邮箱身份回复了邮件"

    r = await client.post(f"/mailboxes/messages/{surface.a_msg}/read", headers=b)
    assert r.status_code == 404, "B 改写了 A 的邮件状态"

    # 属主可用
    r = await client.get("/mailboxes", headers=a)
    assert r.status_code == 200 and surface.a_mbid in [
        m["id"] for m in r.json()], "A 看不到自己的邮箱"

    r = await client.get("/mailboxes/messages", headers=a)
    assert r.status_code == 200
    assert [m["subject"] for m in r.json()] == ["A 的机密来信"], \
        "A 的收件箱内容不对"

    r = await client.post(f"/mailboxes/messages/{surface.a_msg}/reply",
                          json={"body": "收到"}, headers=a)
    assert r.status_code == 200, f"A 回复不了自己的邮件：{r.text[:160]}"

    r = await client.get("/mailboxes/messages", headers=a)
    assert {m["direction"] for m in r.json()} == {"in", "out"}, \
        "A 的回复没有进入自己的收件箱"


# ---------------------------------------------------------------------------
# app_alias：/installed 必须校验 company_id 归属
# ---------------------------------------------------------------------------

async def test_app_alias_installed_is_tenant_scoped(client, surface):
    """B 不能借 A 的 company_id 读到 A 覆盖后的本机应用路径。"""
    a, b = surface.a["headers"], surface.b["headers"]

    r = await client.get(f"/app-alias/installed?company_id={surface.a_co}",
                         headers=b)
    assert r.status_code == 404, "B 读到了 A 公司覆盖后的本机应用路径"

    r = await client.get(f"/app-alias/installed?company_id={surface.a_co}",
                         headers=a)
    assert r.status_code == 200, f"A 读不到自己的已装应用：{r.text[:160]}"
    body = r.json()
    assert "platform" in body and isinstance(body.get("installed"), list), \
        "A 的已装应用响应结构不对"

    paths = {i["name"]: i["path"] for i in body["installed"]}
    if surface.alias_target in paths:
        assert paths[surface.alias_target] == "/tmp/a-only-app", \
            "A 自己的别名路径覆盖没有生效"


# ===========================================================================
# M1：债务白名单逐条转行为用例
#
# `test_permissions.py` 的 `_READ_DEBT_ALLOWLIST` 里还剩 47 条"静态扫描认为
# 没有归属校验"的 company_id 端点。这一节把其中有判定价值的部分逐条写成
# 真实双租户探针：A 建资源，B 拿自己的 token + A 的 id/company_id 去读。
#
# 结论分两类：
#   * 实现里其实已有归属校验（只是扫描器词表没覆盖到，例如
#     `_assert_selectable_company` / `assert_agent_owned`）→ 用例正常通过，
#     对应条目已从 allowlist 删除；
#   * 探针确认**真的能读到** → 不删除 allowlist 条目，用 known_leak 标记 +
#     运行时 xfail 记录，交由主 Agent 修实现（绝不把泄露写成期望值）。
#
# 运行时 xfail 而不是装饰器 xfail：只有"真的返回 200"才 xfail，夹具/路由
# 写错仍然会红，避免把配置错误一起吞成 xfail。
# ===========================================================================


def _require_404(resp, what: str) -> None:
    """B 访问 A 的资源必须 404；返回 200 则登记为已知漏洞并 xfail。"""
    if resp.status_code == 404:
        return
    if resp.status_code == 200:
        pytest.xfail(
            f"known_leak: {what} 未做归属校验，B 用 A 的 id/company_id 读到了数据"
            f"（{resp.text[:120]}）")
    raise AssertionError(
        f"{what} 期望 404，实得 {resp.status_code} {resp.text[:160]}")


@pytest.fixture
async def read_surface(client, tenants) -> SimpleNamespace:
    """A 公司的一批真实资源——B 的攻击目标；同时给 B 备一份对照。"""
    a, b = tenants.alice, tenants.bob
    a_co, b_co = tenants.alice_co["id"], tenants.bob_co["id"]

    a_agent = await _make_agent(client, a, a_co, "A 的员工")
    a_agent2 = await _make_agent(client, a, a_co, "A 的员工二")
    b_agent = await _make_agent(client, b, b_co, "B 的员工")

    r = await client.post(f"/automations?company_id={a_co}", json={
        "name": "A 的自动化机密", "steps": [{"type": "noop"}]},
        headers=a["headers"])
    assert r.status_code == 201, r.text
    a_automation = r.json()["id"]

    r = await client.post(f"/schedules?company_id={a_co}", json={
        "name": "A 的定时任务机密", "trigger_type": "interval", "seconds": 600,
        "action_type": "snapshot_performance"}, headers=a["headers"])
    assert r.status_code == 200, r.text
    a_schedule = r.json()["id"]

    r = await client.post(f"/skills/custom?company_id={a_co}", json={
        "name": "A 专属技能机密", "description": "仅 A 可见"},
        headers=a["headers"])
    assert r.status_code == 201, r.text
    a_skill = r.json()["id"]

    r = await client.post(
        f"/collaboration/collaborations?company_id={a_co}", json={
            "source_agent_id": a_agent, "target_agent_id": a_agent2},
        headers=a["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/collaboration/messages?company_id={a_co}", json={
            "sender_agent_id": a_agent, "message_type": "PROGRESS",
            "title": "A 的协作消息机密", "content": "采购底价 800 万",
            "receiver_ids": [a_agent2]}, headers=a["headers"])
    assert r.status_code == 200, r.text
    a_message = r.json()["id"]

    r = await client.post(
        f"/reviews/cycles?company_id={a_co}&name=A机密考核周期",
        headers=a["headers"])
    assert r.status_code == 201, r.text

    r = await client.put(
        f"/governance/autonomous-settings?company_id={a_co}",
        json={"level": 3}, headers=a["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/governance/improvements?company_id={a_co}", json={
            "agent_id": a_agent, "what_improved": "A 的机密改进提案"},
        headers=a["headers"])
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/governance/save-company-as-template?company_id={a_co}"
        f"&key=a-secret-tpl&name=A机密模板", headers=a["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/memory/remember?company_id={a_co}", json={
        "agent_id": a_agent, "memory_type": "EPISODIC",
        "content": "A 的机密记忆：采购底价 800 万"}, headers=a["headers"])
    assert r.status_code == 200 and r.json().get("stored"), r.text

    r = await client.post(f"/app-alias/rules?company_id={a_co}", json={
        "alias": "打开a机密", "app_name": "Finder",
        "app_path": "/tmp/a-only-app"}, headers=a["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/external-agents?company_id={a_co}", json={
        "name": "A 的外部接入机密", "provider_type": "NATIVE"},
        headers=a["headers"])
    assert r.status_code == 201, r.text
    a_conn = r.json()["id"]

    async with AsyncSessionLocal() as db:
        a_expert = str(uuid.uuid4())
        db.add(ExpertTemplate(
            id=a_expert, company_id=a_co, source_slug="a-secret-expert",
            name="A 机密专家", department="general",
            description="仅 A 可见", system_prompt="机密人设",
            raw_md="A 的机密专家原文", deliverables=[], enabled=True))
        db.add(SkillNode(
            id=str(uuid.uuid4()), company_id=a_co,
            key="a-secret-node", name="A 机密技能节点"))
        db.add(BrowserActionLog(
            id=str(uuid.uuid4()), company_id=a_co, agent_id=a_agent,
            action_type="fetch", url="https://a-secret.example.com",
            allowed=True, status_code=200))
        await db.commit()

    return SimpleNamespace(
        a=a, b=b, a_co=a_co, b_co=b_co, a_agent=a_agent, a_agent2=a_agent2,
        b_agent=b_agent, a_automation=a_automation, a_schedule=a_schedule,
        a_skill=a_skill, a_message=a_message, a_expert=a_expert,
        a_conn=a_conn)


# ---------------------------------------------------------------------------
# 已带归属校验：正常通过，对应 allowlist 条目已删除
# ---------------------------------------------------------------------------

async def test_external_agents_are_tenant_scoped(client, read_surface):
    """external_agents 用 assert_company_owned；扫描器认了，但此前无行为用例。"""
    s = read_surface
    b = s.b["headers"]

    r = await client.post(f"/external-agents?company_id={s.a_co}", json={
        "name": "B 冒充", "provider_type": "NATIVE"}, headers=b)
    assert r.status_code == 404, "B 在 A 公司注册了外部 Agent"

    r = await client.get(f"/external-agents?company_id={s.a_co}", headers=b)
    assert r.status_code == 404, "B 列出了 A 公司的外部接入配置"

    r = await client.get(f"/external-agents?company_id={s.a_co}",
                         headers=s.a["headers"])
    assert r.status_code == 200 and [c["id"] for c in r.json()] == [s.a_conn], \
        f"A 读不到自己的外部接入或数据串了：{r.text[:160]}"


async def test_browser_agent_logs_are_tenant_scoped(client, read_surface):
    """browser_agent 用 _assert_selectable_company（服务端推导可访问公司）。"""
    s = read_surface
    b = s.b["headers"]

    r = await client.get(f"/browser-agent/logs?company_id={s.a_co}", headers=b)
    assert r.status_code == 404, "B 读了 A 公司的浏览器抓取日志"

    r = await client.post(f"/browser-agent/fetch?company_id={s.a_co}",
                          json={"url": "https://example.com"}, headers=b)
    assert r.status_code == 404, "B 以 A 公司身份发起抓取（归属校验在抓取之前）"

    r = await client.get(f"/browser-agent/logs?company_id={s.a_co}",
                         headers=s.a["headers"])
    assert r.status_code == 200 and any(
        row["url"] == "https://a-secret.example.com" for row in r.json()), \
        f"A 看不到自己的抓取日志：{r.text[:160]}"


async def test_training_upload_material_rejects_foreign_agent(client, read_surface):
    """upload-material 经 _agent()→assert_agent_owned 校验；扫描器词表漏了它。"""
    s = read_surface
    files = {"file": ("a-material.txt", b"A secret material", "text/plain")}

    r = await client.post(
        f"/training/upload-material?agent_id={s.a_agent}&company_id={s.a_co}",
        files=files, headers=s.b["headers"])
    assert r.status_code == 404, "B 往 A 员工的私人知识库上传了训练材料"

    r = await client.post(
        f"/training/upload-material?agent_id={s.a_agent}&company_id={s.a_co}",
        files={"file": ("a-material.txt", b"A own material", "text/plain")},
        headers=s.a["headers"])
    assert r.status_code == 200 and r.json()["doc_id"], \
        f"A 上传不了自己的训练材料：{r.text[:160]}"


async def test_app_alias_platform_carries_no_tenant_data(client, read_surface):
    """/app-alias/platform 只返回本机 OS 元信息，传入的 company_id 根本没被用。

    因此它不是越权读，只是静态扫描按"参数名像 company_id"误报；用具例把
    "不读任何租户数据"钉住，以后若有人往里塞别名/路径，会立刻变红。
    """
    s = read_surface
    r = await client.get(f"/app-alias/platform?company_id={s.a_co}",
                         headers=s.b["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"os", "os_name", "os_version", "machine"}, \
        f"platform 返回了租户数据字段：{body}"
    assert "A 机密" not in r.text and "/tmp/a-only-app" not in r.text, \
        "platform 端点泄露了 A 公司的别名/路径"

    own = await client.get(f"/app-alias/platform?company_id={s.b_co}",
                           headers=s.b["headers"])
    assert own.json() == body, "platform 响应随 company_id 变化，说明读了租户数据"


# ---------------------------------------------------------------------------
# 曾经的真实越权读：实现已补归属校验，这里钉死为"必须 404"
#
# 这些用例此前用 known_leak + 运行时 xfail 记录（当时确实能读到 A 的数据）。
# 修法：受影响 router 挂 require_company_scope（按 company_id 校验归属，
# 不通过 404），app-alias 是逐端点加 assert_company_owned（/platform 不读
# 租户数据，故不挂）。现在改为严格 404 断言——再退回去会立刻变红。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,what", [
    ("/experts", "GET /experts"),
    ("/collaboration/messages", "GET /collaboration/messages"),
    ("/schedules", "GET /schedules"),
    ("/memory/recall", "GET /memory/recall"),
    ("/reflection/agents-overview", "GET /reflection/agents-overview"),
    ("/governance/autonomous-settings", "GET /governance/autonomous-settings"),
    ("/governance/autonomous-overview", "GET /governance/autonomous-overview"),
    ("/governance/self-improvement-cases", "GET /governance/self-improvement-cases"),
    ("/governance/templates", "GET /governance/templates"),
    ("/skills", "GET /skills"),
    ("/skill-growth/tree", "GET /skill-growth/tree"),
    ("/app-alias/rules", "GET /app-alias/rules"),
    ("/automations", "GET /automations"),
])
async def test_read_endpoints_reject_foreign_company(client, read_surface,
                                                     path, what):
    """B 带 A 的 company_id 读列表：一律 404，且不得回显任何 A 的内容。"""
    import urllib.parse

    s = read_surface
    if path == "/memory/recall":
        qs = f"?company_id={s.a_co}&agent_id={s.a_agent}&q=采购"
    else:
        qs = f"?company_id={s.a_co}"
    r = await client.get(path + qs, headers=s.b["headers"])
    assert r.status_code == 404, f"{what} 未做归属校验：{r.status_code} {r.text[:160]}"
    assert "机密" not in r.text and s.a_co not in r.text


async def test_expert_detail_rejects_foreign_company(client, read_surface):
    s = read_surface
    r = await client.get(f"/experts/{s.a_expert}?company_id={s.a_co}",
                         headers=s.b["headers"])
    assert r.status_code == 404, f"GET /experts/{{eid}} 越权：{r.status_code} {r.text[:160]}"


async def test_collaboration_message_detail_rejects_foreign_company(client,
                                                                    read_surface):
    s = read_surface
    r = await client.get(f"/collaboration/messages/{s.a_message}"
                         f"?company_id={s.a_co}", headers=s.b["headers"])
    assert r.status_code == 404, f"GET /collaboration/messages/{{id}} 越权：{r.status_code}"


async def test_collaboration_graph_rejects_foreign_company(client, read_surface):
    s = read_surface
    r = await client.get(f"/collaboration/agents/{s.a_agent}/graph"
                         f"?company_id={s.a_co}", headers=s.b["headers"])
    assert r.status_code == 404, f"GET /collaboration/agents/{{id}}/graph 越权：{r.status_code}"


async def test_review_cycle_cannot_be_created_in_foreign_company(client,
                                                                read_surface):
    s = read_surface
    r = await client.post(
        f"/reviews/cycles?company_id={s.a_co}&name=B投毒周期",
        headers=s.b["headers"])
    assert r.status_code == 404, f"POST /reviews/cycles 越权：{r.status_code} {r.text[:160]}"
