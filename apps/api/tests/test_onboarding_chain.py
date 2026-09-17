"""冷启动与主链路的前置条件守卫。

覆盖两类问题：

1. **自检口径必须与运行时一致**
   `GET /models/diagnostics` 是前端引导向导用来判断"能不能开工"的依据。
   它原先只看 provider 的默认环境变量，不看模型自己配置的 `api_key_env`，
   于是用户把密钥名填成自定义值时，界面显示"缺密钥"而系统其实可用——
   用户会去改一个本来正确的配置。

2. **失败必须留下可查的原因**
   用户看到的应该是"为什么失败"，而不是一句"失败了"。
   这里既验证证据是否落库，也验证错误信息是否**可操作**。
"""
from __future__ import annotations

import asyncio

import pytest

MODEL_ID_ENV = "E2E_CUSTOM_MODEL_KEY"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 1) 模型自检
# ---------------------------------------------------------------------------

async def _create_model(client, user, *, api_key_env: str) -> str:
    r = await client.post("/models", json={
        "name": "自检用模型",
        "provider_type": "openai_compat",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key_env": api_key_env,
        "model_id": "some-model",
    }, headers=user["headers"])
    assert r.status_code == 201, f"登记模型失败: {r.status_code} {r.text}"
    return r.json()["id"]


async def test_diagnostics_honours_custom_env_var(client, make_user, monkeypatch):
    """模型自带的 api_key_env 已就位时，自检必须判定为可用。

    回归守卫：原实现只检查 provider 的默认环境变量，造成"可用却报缺密钥"。
    """
    monkeypatch.setenv(MODEL_ID_ENV, "not-a-real-key")
    user = await make_user()
    model_id = await _create_model(client, user, api_key_env=MODEL_ID_ENV)

    r = await client.get("/models/diagnostics", headers=user["headers"])
    assert r.status_code == 200
    d = r.json()

    assert d["ready"] is True, f"自检未识别出自定义环境变量：{d}"
    assert model_id in d["usable_model_ids"]
    openai = [p for p in d["providers"] if p["provider_type"] == "openai_compat"][0]
    assert openai["key_present"] is True, "逐 provider 明细与总体判定矛盾"
    assert openai["models_ready"] == 1
    assert not d["hint"], f"就绪时不应给出补救提示：{d['hint']}"


async def test_diagnostics_never_leaks_key_material(client, make_user, monkeypatch):
    """自检只能回答"有没有"，不能回显密钥内容。"""
    secret = "sk-super-secret-value-should-never-appear"
    monkeypatch.setenv(MODEL_ID_ENV, secret)
    user = await make_user()
    await _create_model(client, user, api_key_env=MODEL_ID_ENV)

    r = await client.get("/models/diagnostics", headers=user["headers"])
    assert secret not in r.text, "自检响应里出现了密钥明文"


async def test_diagnostics_missing_key_gives_actionable_hint(client, make_user,
                                                             monkeypatch):
    """缺密钥时，提示里必须写清"该配哪个变量"，否则用户无从下手。"""
    monkeypatch.delenv("MISSING_MODEL_KEY_FOR_TEST", raising=False)
    user = await make_user()
    await _create_model(client, user, api_key_env="MISSING_MODEL_KEY_FOR_TEST")

    r = await client.get("/models/diagnostics", headers=user["headers"])
    d = r.json()
    assert d["ready"] is False
    assert "MISSING_MODEL_KEY_FOR_TEST" in (d["hint"] or ""), \
        f"提示未指明需要配置的变量名：{d['hint']!r}"


# ---------------------------------------------------------------------------
# 2) 创建项目的 company_id（请求体与查询参数双通道）
# ---------------------------------------------------------------------------

async def test_project_accepts_company_id_in_body(client, make_user, make_company):
    """与 /agents、/tasks 一致：company_id 放在请求体里。"""
    user = await make_user()
    co = await make_company(user)

    r = await client.post("/projects", json={
        "company_id": co["id"], "name": "体式项目"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    assert r.json()["company_id"] == co["id"]


async def test_project_still_accepts_company_id_in_query(client, make_user,
                                                         make_company):
    """向后兼容：既有前端调用使用 ?company_id=...，不能改坏。"""
    user = await make_user()
    co = await make_company(user)

    r = await client.post(f"/projects?company_id={co['id']}",
                          json={"name": "参数式项目"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    assert r.json()["company_id"] == co["id"]


async def test_project_without_company_id_is_rejected_clearly(client, make_user):
    """两处都没给时，要说清缺什么，而不是抛一个看不懂的 500。"""
    user = await make_user()

    r = await client.post("/projects", json={"name": "无归属项目"},
                          headers=user["headers"])
    assert r.status_code == 422
    assert "company_id" in r.text


async def test_project_cannot_attach_to_other_tenants_company(client, tenants):
    """跨租户建项目必须失败（且是 404，不泄露"该公司存在"）。"""
    r = await client.post("/projects", json={
        "company_id": tenants.bob_co["id"], "name": "越权项目"},
        headers=tenants.alice["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3) 失败可见性
# ---------------------------------------------------------------------------

async def _wait_terminal(client, task_id: str, headers: dict,
                         timeout: float = 60.0) -> dict:
    """轮询直到任务进入终态。"""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/tasks/{task_id}", headers=headers)
        last = r.json()
        if last.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(f"任务在 {timeout}s 内未进入终态，当前状态={last.get('status')}")


async def test_failed_task_leaves_traceable_evidence(client, make_user, make_company):
    """员工没有可用模型时任务必然失败——但必须留下**可查、可读**的原因。

    回归守卫：失败路径原先把失败原因写进 execution 与事件表，却在同一个未提交
    事务里；外层为了清理状态执行 rollback，把证据一起撤销了。结果是用户看到
    "任务失败"，点进执行记录却什么都没有，错误信息也只存在于服务端日志。
    """
    user = await make_user()
    co = await make_company(user)

    r = await client.post("/agents", json={
        "company_id": co["id"], "name": "未配模型的员工"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    r = await client.post("/tasks", json={
        "company_id": co["id"], "title": "注定失败的任务",
        "assigned_agent_id": agent_id}, headers=user["headers"])
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = await client.post(f"/tasks/{task_id}/start", headers=user["headers"])
    assert r.status_code == 200, r.text

    final = await _wait_terminal(client, task_id, user["headers"])
    assert final["status"] == "FAILED", f"预期失败，实际 {final['status']}"

    # 证据 1：执行记录必须留下，且带着失败原因
    r = await client.get(f"/tasks/{task_id}/executions", headers=user["headers"])
    executions = r.json()
    assert executions, "任务失败了，却没有任何执行记录——失败无迹可查"
    failed = [e for e in executions if e["status"] == "failed"]
    assert failed, f"没有状态为 failed 的执行记录：{[e['status'] for e in executions]}"
    error_text = next((e.get("error") or "" for e in failed if e.get("error")), "")
    assert error_text, "执行记录里没有失败原因"

    # 证据 2：失败原因要**可操作**——必须点出"模型"这一根因，
    # 而不是 "All configured models failed: None" 这种只描述现象的文字。
    assert "模型" in error_text, (
        f"失败原因对用户不可操作，无法据此修复：{error_text!r}")

    # 证据 3：事件流在**任务维度**也留痕。用户排查时是按任务查的，
    # 失败原因如果只挂在员工上，"这个任务为什么失败"就无从检索。
    r = await client.get("/events?event_type=task.failed&limit=50",
                         headers=user["headers"])
    hits = [e for e in r.json() if e.get("entity_id") == task_id]
    assert hits, "任务维度的失败事件未落库——用户无法按任务检索失败原因"

    # 证据 4：员工维度的事件也要能关联回本次任务
    r = await client.get("/events?event_type=agent.failed&limit=50",
                         headers=user["headers"])
    assert any((e.get("payload") or {}).get("task_id") == task_id
               for e in r.json()), "agent.failed 事件未关联到本次任务"
