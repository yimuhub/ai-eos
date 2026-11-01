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
import re
import uuid

import httpx
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


# ---------------------------------------------------------------------------
# Prometheus RED / LLM / 任务队列指标
# ---------------------------------------------------------------------------

async def _scrape(client) -> str:
    r = await client.get("/metrics/prometheus")
    assert r.status_code == 200, r.text
    assert "text/plain" in r.headers.get("content-type", "")
    return r.text


async def test_http_requests_total_counts_health_200(client):
    """GET /health 之后，抓取文本里必须有 status="200" 的样本。"""
    assert (await client.get("/health")).status_code == 200
    text = await _scrape(client)

    assert "# TYPE aieos_http_requests_total counter" in text
    assert ('aieos_http_requests_total{method="GET",path="/health",'
            'status="200"}') in text
    # 原有 4 个探针指标必须保留（扩展而不是替换）。
    for legacy in ("aieos_up", "aieos_process_start_time_seconds",
                   "aieos_db_up", "aieos_db_probe_duration_seconds"):
        assert legacy in text


async def test_unknown_route_is_unmatched_not_high_cardinality(client):
    """未匹配路由必须归一到 unmatched，不能把随机 id 变成标签值。"""
    random_id = uuid.uuid4().hex
    r = await client.get(f"/definitely-not-a-route-{random_id}")
    assert r.status_code == 404

    text = await _scrape(client)
    assert random_id not in text, "真实路径被当成标签值，基数会无界增长"
    assert 'aieos_http_requests_total{method="GET",path="unmatched",' \
           'status="404"}' in text


async def test_http_duration_histogram_count_increments(client):
    """直方图 _count 必须随请求递增，而不是只有 bucket 没有总数。"""
    pattern = re.compile(
        r'aieos_http_request_duration_seconds_count'
        r'\{method="GET",path="/health"\} (\d+)')

    await client.get("/health")
    first = pattern.search(await _scrape(client))
    assert first is not None, "直方图 _count 样本缺失"

    await client.get("/health")
    second = pattern.search(await _scrape(client))
    assert second is not None
    assert int(second.group(1)) == int(first.group(1)) + 1


async def test_task_queue_depth_is_exposed(client):
    """任务队列深度在空闲时也应暴露 0，而不是等第一单任务才出现。"""
    text = await _scrape(client)
    assert 'aieos_task_queue_depth{kind="pending"}' in text
    assert 'aieos_task_queue_depth{kind="running"}' in text


async def test_metrics_rejects_non_loopback_without_token(monkeypatch):
    """鉴权行为保持不变：未配 token 且非回环来源 → 404。"""
    from app.core.config import settings
    from app.main import app as asgi_app

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "metrics_token", "")

    transport = httpx.ASGITransport(
        app=asgi_app, client=("203.0.113.9", 45678))
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver") as remote:
        r = await remote.get("/metrics/prometheus")
    assert r.status_code == 404, f"非回环来源不应拿到指标：{r.status_code}"


async def test_metrics_requires_token_when_configured(monkeypatch):
    """配了 METRICS_TOKEN 时，非回环来源不带 token 必须 401。"""
    from app.core.config import settings
    from app.main import app as asgi_app

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "metrics_token", "test-metrics-token")

    transport = httpx.ASGITransport(
        app=asgi_app, client=("203.0.113.9", 45678))
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver") as remote:
        denied = await remote.get("/metrics/prometheus")
        allowed = await remote.get(
            "/metrics/prometheus",
            headers={"Authorization": "Bearer test-metrics-token"})

    assert denied.status_code == 401
    assert "www-authenticate" in {k.lower() for k in denied.headers}
    assert allowed.status_code == 200
    assert "aieos_http_requests_total" in allowed.text


async def test_prometheus_label_values_are_escaped():
    """标签值里的引号、反斜杠、换行必须转义，否则会破坏抓取文本。"""
    from app.core import metrics

    counter = metrics.Counter(
        "aieos_test_escape_total", "escape probe", ("label",))
    counter.inc(1, label='a"b\\c\nd')

    text = metrics.render_prometheus([counter])
    assert 'aieos_test_escape_total{label="a\\"b\\\\c\\nd"} 1' in text
    # 原始换行若未转义，会多切出一行，导致样本被解析器丢弃。
    assert text.count("aieos_test_escape_total{") == 1


async def test_llm_metrics_record_success_tokens_and_errors():
    """LLM 埋点：成功记 tokens，失败也计入 error，且不改变调用结果。"""
    from types import SimpleNamespace

    from app.core import metrics
    from app.model_gateway import _observe_llm_call

    row = SimpleNamespace(
        provider_type=SimpleNamespace(value="openai_compat"),
        model_id="fake-model",
        name="假模型",
    )
    _observe_llm_call(row, "success", 0.12,
                      {"prompt_tokens": 11, "completion_tokens": 7})
    _observe_llm_call(row, "error", 0.03)

    text = metrics.render_prometheus()
    assert ('aieos_llm_requests_total{provider="openai_compat",'
            'model="fake-model",outcome="success"}') in text
    assert ('aieos_llm_requests_total{provider="openai_compat",'
            'model="fake-model",outcome="error"}') in text
    assert ('aieos_llm_tokens_total{provider="openai_compat",'
            'model="fake-model",kind="input"}') in text
    assert ('aieos_llm_tokens_total{provider="openai_compat",'
            'model="fake-model",kind="output"}') in text
