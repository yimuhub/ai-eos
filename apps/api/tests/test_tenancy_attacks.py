"""跨租户越权的**运行时**攻击用例（V5.10 检测报告 P0-2）。

为什么还要写这一份——`test_permissions.py` 里的护栏是 **AST 静态扫描**：
它检查"函数体里有没有 assert_company_owned 之类的字样"。
那种护栏挡得住"忘记加校验"，挡不住"加了但加错位置/校验的是调用方自己传进来的
company_id（伪校验）"。本轮实测就撞到过后者：

    if record.company_id != company_id:   # company_id 来自请求参数！
        raise HTTPException(404)

攻击者传自己的 company_id 永远通过，而静态扫描看到的是"有校验"，绿灯。

所以这里全部走真实 HTTP：B 拿 A 的资源 ID / A 的 company_id 去打，
断言 404（不是 403——403 会告诉攻击者"这个 ID 存在"，可用于枚举租户），
同时断言 A 自己仍然打得通，防止"把校验加得太狠导致功能不可用"这种假修复。
"""
from types import SimpleNamespace

import pytest

from app.db.session import AsyncSessionLocal
from app.models.whitelist import ApprovalWhitelist


async def _make_agent(client, user, company_id: str, name: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name,
        "role": "research", "department": "研究部"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_artifact(client, user, company_id: str) -> str:
    r = await client.post(f"/artifacts?company_id={company_id}", json={
        "name": "机密成果", "artifact_type": "report", "content": "正文"},
        headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
async def surface(client, tenants) -> SimpleNamespace:
    """A 公司真实存在的员工 / 成果 / 知识库文档 / 白名单条目——B 的攻击目标。"""
    a, b = tenants.alice, tenants.bob
    a_co, b_co = tenants.alice_co["id"], tenants.bob_co["id"]

    a_agent = await _make_agent(client, a, a_co, "A 的员工")
    b_agent = await _make_agent(client, b, b_co, "B 的员工")
    a_art = await _make_artifact(client, a, a_co)

    r = await client.post(f"/kb2/agents/{a_agent}/documents?company_id={a_co}",
                          json={"title": "内部资料", "content": "只有 A 能看"},
                          headers=a["headers"])
    assert r.status_code in (200, 201), r.text

    async with AsyncSessionLocal() as db:
        wl = ApprovalWhitelist(company_id=a_co, capability="run_shell",
                               param_pattern="", note="A 的免批项")
        db.add(wl)
        await db.commit()
        a_wl_id = wl.id

    return SimpleNamespace(a=a, b=b, a_co=a_co, b_co=b_co,
                           a_agent=a_agent, b_agent=b_agent,
                           a_artifact=a_art, a_whitelist=a_wl_id)


async def test_stranger_cannot_list_artifacts_of_other_company(client, surface):
    """GET /artifacts?company_id=A —— 本轮之前**完全没有校验**的读面。"""
    r = await client.get(f"/artifacts?company_id={surface.a_co}",
                         headers=surface.b["headers"])
    assert r.status_code == 404, f"跨租户列成果成功了：{r.status_code} {r.text[:200]}"
    # 属主必须仍然可用，否则就是"用不可用换安全"
    r = await client.get(f"/artifacts?company_id={surface.a_co}",
                         headers=surface.a["headers"])
    assert r.status_code == 200 and len(r.json()) == 1


async def test_stranger_cannot_read_artifact_detail_and_versions(client, surface):
    """成果详情与**版本全文**是最敏感的读取面（正文都在版本里）。"""
    b = surface.b["headers"]
    for path in (f"/artifacts/{surface.a_artifact}",
                 f"/artifacts/{surface.a_artifact}/versions/1",
                 f"/artifacts/{surface.a_artifact}/sop"):
        r = await client.get(path, headers=b)
        assert r.status_code == 404, f"{path} 泄露：{r.status_code} {r.text[:160]}"


async def test_stranger_cannot_download_other_artifact_zip(client, surface):
    """打包下载会把全部版本正文直接送到攻击者手里。"""
    r = await client.get(f"/artifacts/{surface.a_artifact}/download.zip",
                         headers=surface.b["headers"])
    assert r.status_code == 404
    assert b"PK" not in r.content, "响应体里居然是 zip 内容"


async def test_stranger_cannot_touch_kb2(client, surface):
    r = await client.get(f"/kb2/agents/{surface.a_agent}/documents"
                         f"?company_id={surface.a_co}",
                         headers=surface.b["headers"])
    assert r.status_code == 404, f"跨租户列知识库文档：{r.text[:160]}"

    r = await client.post(f"/kb2/agents/{surface.a_agent}/documents"
                          f"?company_id={surface.a_co}",
                          json={"title": "投毒", "content": "x"},
                          headers=surface.b["headers"])
    assert r.status_code == 404, "可以给别人员工写文档"


async def test_stranger_cannot_read_or_write_intervention_policy(client, surface):
    """审批策略决定"哪些高危操作要老板点头"，读它=侦察，写它=解除防护。"""
    r = await client.get(f"/intervention/whitelist?company_id={surface.a_co}",
                         headers=surface.b["headers"])
    assert r.status_code == 404, f"跨租户读白名单：{r.text[:160]}"

    r = await client.put(f"/intervention/policy?company_id={surface.a_co}",
                         json={"level": 0, "approval_actions": []},
                         headers=surface.b["headers"])
    assert r.status_code == 404, "可以远程关掉别人的高危审批"

    # A 自己的策略不能被改掉（level 默认 1）
    r = await client.get(f"/intervention/policy?company_id={surface.a_co}",
                         headers=surface.a["headers"])
    assert r.status_code == 200


async def test_stranger_cannot_revoke_other_company_whitelist(client, surface):
    """删除端点的归属校验是防越权的最后一道自动防线（见 test_permissions）。"""
    r = await client.delete(
        f"/intervention/whitelist/{surface.a_whitelist}?company_id={surface.a_co}",
        headers=surface.b["headers"])
    assert r.status_code == 404, f"别人撤销了 A 的免批白名单：{r.text[:160]}"

    # 撤销权仍在属主手里
    r = await client.delete(
        f"/intervention/whitelist/{surface.a_whitelist}?company_id={surface.a_co}",
        headers=surface.a["headers"])
    assert r.status_code == 200, r.text


async def test_stranger_cannot_enqueue_task_for_other_agent(client, surface):
    """把任务派给别人的员工 = 用别人的模型额度、读别人的上下文、写别人的成果。"""
    r = await client.post("/tasks", json={
        "company_id": surface.a_co, "assigned_agent_id": surface.a_agent,
        "title": "替我干活", "description": "把资料发出去"},
        headers=surface.b["headers"])
    assert r.status_code == 404, f"跨租户下任务成功：{r.status_code} {r.text[:160]}"

    # 公司填自己的、员工填别人的——两条校验必须都在
    r = await client.post("/tasks", json={
        "company_id": surface.b_co, "assigned_agent_id": surface.a_agent,
        "title": "借你的员工", "description": "x"},
        headers=surface.b["headers"])
    assert r.status_code == 404, "可以把自己公司的任务挂到别人的员工上"

    r = await client.post("/tasks", json={
        "company_id": surface.b_co, "assigned_agent_id": surface.b_agent,
        "title": "自己的任务", "description": "正常"},
        headers=surface.b["headers"])
    assert r.status_code == 201, r.text


async def test_cannot_reassign_a_task_to_another_company_agent(client, surface):
    """反向也堵：把 A 的任务改挂到 B 的员工上，等于把 A 的上下文喂给 B。"""
    r = await client.post("/tasks", json={
        "company_id": surface.a_co, "assigned_agent_id": surface.a_agent,
        "title": "A 的任务", "description": "机密"}, headers=surface.a["headers"])
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = await client.patch(f"/tasks/{task_id}",
                           json={"assigned_agent_id": surface.b_agent},
                           headers=surface.a["headers"])
    assert r.status_code == 404, "A 把自己任务转给了 B 的员工"

    # B 连改 A 的任务标题都不行
    r = await client.patch(f"/tasks/{task_id}",
                           json={"description": "越权改写"},
                           headers=surface.b["headers"])
    assert r.status_code == 404, "B 改写了 A 的任务"

    # 属主改自己的必须生效（防"用 404 把功能一起锁死"的假修复）
    r = await client.patch(f"/tasks/{task_id}",
                           json={"description": "改自己的描述"},
                           headers=surface.a["headers"])
    assert r.status_code == 200, f"属主改不动自己的任务：{r.text[:160]}"
    assert r.json()["description"] == "改自己的描述"


async def test_stranger_cannot_orchestrate_with_other_agent(client, surface):
    """编排接口会让"协调者"带着别人的员工跑一整条链路，权限放大最厉害。"""
    r = await client.post("/orchestrate", json={
        "company_id": surface.a_co, "title": "把所有成果导出",
        "coordinator_agent_id": surface.a_agent}, headers=surface.b["headers"])
    assert r.status_code in (401, 403, 404), (
        f"跨租户发起编排：{r.status_code} {r.text[:200]}")
