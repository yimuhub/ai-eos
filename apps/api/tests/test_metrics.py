"""度量体系的守卫。

## 这一组测试真正想钉住的是什么

不是"字段有没有返回"，而是**口径**：

* 没有任何样本时，成功率必须是 `null` 而不是 `0.0`。
  否则一个刚建好、还没跑过任务的公司，看板上会显示"成功率 0%"，
  用户会以为系统一直在失败。把"没数据"读成"全失败"是度量看板最经典的错。
* 有 1 次失败时，成功率必须如实是 `0.0`——它是**真实结论**，不是缺数据。

这两件事看起来只是 0 和 null 的区别，但决定了用户会不会被自己的看板误导。
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def _failed_task(client, user, company) -> str:
    """造一个必然失败的任务：员工没有任何可用模型。"""
    r = await client.post("/agents", json={
        "company_id": company["id"], "name": "无模型员工"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    r = await client.post("/tasks", json={
        "company_id": company["id"], "title": "度量用失败任务",
        "assigned_agent_id": agent_id}, headers=user["headers"])
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = await client.post(f"/tasks/{task_id}/start", headers=user["headers"])
    assert r.status_code == 200, r.text

    # 等后台执行结束（失败是即时的，无需模型）
    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        body = (await client.get(f"/tasks/{task_id}",
                                 headers=user["headers"])).json()
        if body.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            assert body["status"] == "FAILED", f"预期失败，实际 {body['status']}"
            return task_id
        await asyncio.sleep(0.5)
    raise AssertionError("任务未在超时内进入终态")


async def test_empty_company_reports_null_not_zero(client, make_user, make_company):
    """还没有样本时，所有比率必须是 null——"没数据"不是"全失败"。"""
    user = await make_user()
    co = await make_company(user)

    r = await client.get(f"/metrics/overview?company_id={co['id']}",
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    m = r.json()

    assert m["tasks"]["total"] == 0
    assert m["tasks"]["success_rate"] is None, \
        f"无样本时成功率应为本 null，实际 {m['tasks']['success_rate']}"
    assert m["executions"]["success_rate"] is None
    assert m["model_calls"]["success_rate"] is None
    assert m["executions"]["avg_duration_seconds"] is None
    assert m["model_calls"]["avg_latency_ms"] is None
    assert m["failure_reasons"] == []
    assert m["failure_by_agent"] == []


async def test_rate_is_zero_when_a_real_failure_happened(client, make_user,
                                                         make_company):
    """有真实失败样本时，0.0 是**结论**，必须如实给出（而不是 null）。"""
    user = await make_user()
    co = await make_company(user)
    await _failed_task(client, user, co)

    m = (await client.get(f"/metrics/overview?company_id={co['id']}",
                          headers=user["headers"])).json()

    assert m["tasks"]["failed"] == 1
    assert m["tasks"]["success_rate"] == 0.0
    assert m["executions"]["failed"] == 1
    assert m["executions"]["success_rate"] == 0.0


async def test_failure_is_attributable(client, make_user, make_company):
    """失败必须能回答"为什么"和"谁"——否则看板只有结论没有方向。"""
    user = await make_user()
    co = await make_company(user)
    await _failed_task(client, user, co)

    m = (await client.get(f"/metrics/overview?company_id={co['id']}",
                          headers=user["headers"])).json()

    assert m["failure_reasons"], "没有聚合出失败原因"
    reason = m["failure_reasons"][0]
    assert reason["count"] == 1
    assert "模型" in reason["reason"], \
        f"失败原因不可读/不可操作：{reason['reason']!r}"

    assert m["failure_by_agent"], "没有指出失败集中在哪个员工身上"
    assert m["failure_by_agent"][0]["agent_name"] == "无模型员工"


async def test_duration_is_measured(client, make_user, make_company):
    """耗时度量必须真的有数字——它依赖执行记录的起止时间。"""
    user = await make_user()
    co = await make_company(user)
    await _failed_task(client, user, co)

    m = (await client.get(f"/metrics/overview?company_id={co['id']}",
                          headers=user["headers"])).json()

    duration = m["executions"]["avg_duration_seconds"]
    assert duration is not None, "执行耗时没有被测出来"
    assert duration >= 0


async def test_window_excludes_old_samples(client, make_user, make_company):
    """时间窗必须真的生效：days=1 与 days=365 不应总是同一个答案。

    这里只验证边界不会报错且口径一致（造 400 天前的数据需要改系统时间，
    不值得为此引入脆弱测试）。
    """
    user = await make_user()
    co = await make_company(user)
    await _failed_task(client, user, co)

    for days in (1, 7, 365):
        r = await client.get(
            f"/metrics/overview?company_id={co['id']}&days={days}",
            headers=user["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["window_days"] == days

    r = await client.get(f"/metrics/overview?company_id={co['id']}&days=0",
                         headers=user["headers"])
    assert r.status_code == 422, "非法时间窗应被拒绝"


async def test_metrics_are_tenant_scoped(client, tenants):
    """度量是按公司隔离的：看不到别家的数据，也拿不到别家的公司。"""
    r = await client.get(f"/metrics/overview?company_id={tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404

    m = (await client.get(f"/metrics/overview?company_id={tenants.alice_co['id']}",
                          headers=tenants.alice["headers"])).json()
    assert m["company"]["id"] == tenants.alice_co["id"]
    assert m["tasks"]["total"] == 0


async def test_dashboard_returns_404_not_200_with_error_body(client, tenants):
    """看板接口的鉴权失败必须是 404，不能是 200 + {"error": ...}。

    回归守卫：原先返回 200，HTTP 层看不出失败，前端只能靠读 body 里的
    error 字段判断，很容易漏判成"数据都是 0"。
    """
    r = await client.get(f"/dashboard/{tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404, f"期望 404，实际 {r.status_code} {r.text}"
