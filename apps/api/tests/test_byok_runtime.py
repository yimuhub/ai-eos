"""BYOK 真正生效：运行时密钥解析是「公司密钥库 → 环境变量」。

审查报告 7.3 的结论是「客户填了自己的 Key，网关从来不用它」——这些用例
盯的就是那条链：密钥库里有就用它，没有才回落到平台密钥，解不开也不能
把租户的模型调用整片打挂。
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import update

from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.agent import Model, ModelProviderType
from app.models.company_secret import CompanySecret
from app.services import api_key_resolver, secret_vault

pytestmark = pytest.mark.asyncio

# 刻意用自定义变量名：默认变量（OPENAI_COMPAT_API_KEY）可能被开发机的
# 真实 .env 填过，会让"有/没有平台密钥"变得不可控。
ENV_NAME = "AIEOS_PROVIDER_BYOKTEST_API_KEY"
PROVIDER = ModelProviderType.OPENAI_COMPAT


async def _store(company_id: str, name: str, value: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(CompanySecret(company_id=company_id, name=name,
                             ciphertext=secret_vault.encrypt(value)))
        await db.commit()


async def _make_model(company_id: str) -> Model:
    async with AsyncSessionLocal() as db:
        row = Model(company_id=company_id, name="BYOK 测试模型",
                    provider_type=PROVIDER,
                    base_url="http://127.0.0.1:9/v1", model_id="test-model",
                    api_key_env=ENV_NAME, enabled=True)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _provider_key(company_id: str, row: Model) -> str:
    async with AsyncSessionLocal() as db:
        provider = await ModelGateway(db)._provider_for(row, company_id)
        assert provider is not None
        return provider.api_key


async def test_vault_key_overrides_platform_env(tenants, monkeypatch):
    company_id = tenants.alice_co["id"]
    monkeypatch.setenv(ENV_NAME, "platform-env-key")
    await _store(company_id, ENV_NAME, "vault-own-key")

    async with AsyncSessionLocal() as db:
        resolved = await api_key_resolver.resolve(
            db, company_id=company_id, api_key_env=ENV_NAME,
            provider_type=PROVIDER)
    assert resolved.source == api_key_resolver.SOURCE_VAULT
    assert resolved.value == "vault-own-key"

    row = await _make_model(company_id)
    assert await _provider_key(company_id, row) == "vault-own-key"


async def test_falls_back_to_env_without_vault(tenants, monkeypatch):
    company_id = tenants.alice_co["id"]
    monkeypatch.setenv(ENV_NAME, "platform-env-key")

    async with AsyncSessionLocal() as db:
        resolved = await api_key_resolver.resolve(
            db, company_id=company_id, api_key_env=ENV_NAME,
            provider_type=PROVIDER)
    assert resolved.source == api_key_resolver.SOURCE_ENV
    assert resolved.value == "platform-env-key"

    row = await _make_model(company_id)
    assert await _provider_key(company_id, row) == "platform-env-key"


async def test_vault_is_per_company_and_name_matched(tenants, monkeypatch):
    """别的公司的密钥、别的名字的密钥都不能被用上。"""
    alice_co = tenants.alice_co["id"]
    bob_co = tenants.bob_co["id"]
    monkeypatch.setenv(ENV_NAME, "platform-env-key")
    await _store(bob_co, ENV_NAME, "bob-key")
    await _store(alice_co, "SOMETHING_ELSE", "other-secret")

    row = await _make_model(alice_co)
    resolved_value = await _provider_key(alice_co, row)
    assert resolved_value == "platform-env-key", "Bob 的密钥不能进 Alice 的请求"


async def test_undecryptable_secret_degrades_to_env(tenants, monkeypatch):
    company_id = tenants.alice_co["id"]
    monkeypatch.setenv(ENV_NAME, "platform-env-key")
    await _store(company_id, ENV_NAME, "vault-own-key")
    async with AsyncSessionLocal() as db:
        await db.execute(update(CompanySecret).where(
            CompanySecret.company_id == company_id,
            CompanySecret.name == ENV_NAME).values(ciphertext="not-a-fernet"))
        await db.commit()

    row = await _make_model(company_id)
    # 密钥轮换后解不开：要降级到环境变量，而不是让整个租户 401
    assert await _provider_key(company_id, row) == "platform-env-key"


async def test_sensitive_env_name_never_reaches_vault_or_env(tenants, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "server-secret")
    assert api_key_resolver.candidate_names(
        "JWT_SECRET", PROVIDER) == ["OPENAI_COMPAT_API_KEY"]


async def test_verify_probes_upstream_and_never_echoes_key(client, tenants,
                                                           monkeypatch):
    import app.core.http_client as http_client_module

    alice, company_id = tenants.alice, tenants.alice_co["id"]
    plaintext = "vault-probe-key-123"
    r = await client.put("/byok/probe-key",
                         json={"company_id": company_id, "value": plaintext},
                         headers=alice["headers"])
    assert r.status_code == 200, r.text

    calls: list[dict] = []

    class _Resp:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class _Client:
        def __init__(self, outcome):
            self.outcome = outcome

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append({"url": url, "headers": headers or {}})
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return _Resp(self.outcome)

    def _patch(outcome):
        monkeypatch.setattr(http_client_module, "http_client",
                            lambda **kwargs: _Client(outcome))

    # 字面公网 IP：校验出站地址时不需要 DNS，用例离线也能跑
    body = {"company_id": company_id, "provider": "openai_compat",
            "base_url": "http://93.184.216.34"}

    _patch(200)
    r = await client.post("/byok/probe-key/verify", json=body,
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True and r.json()["reachable"] is True
    assert calls[-1]["url"] == "http://93.184.216.34/models"
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {plaintext}"
    assert plaintext not in r.text

    _patch(401)
    r = await client.post("/byok/probe-key/verify", json=body,
                          headers=alice["headers"])
    assert r.json()["valid"] is False and r.json()["reachable"] is True
    assert plaintext not in r.text

    _patch(httpx.ConnectTimeout("boom"))
    r = await client.post("/byok/probe-key/verify", json=body,
                          headers=alice["headers"])
    assert r.json()["reachable"] is False and r.json()["valid"] is None
    assert plaintext not in r.text


async def test_verify_without_provider_stays_offline(client, tenants,
                                                     monkeypatch):
    alice, company_id = tenants.alice, tenants.alice_co["id"]
    await client.put("/byok/offline-key",
                     json={"company_id": company_id, "value": "vault-offline"},
                     headers=alice["headers"])

    def _explode(**kwargs):
        raise AssertionError("没给 provider 时不应发起任何外部请求")

    import app.core.http_client as http_client_module
    monkeypatch.setattr(http_client_module, "http_client", _explode)

    r = await client.post("/byok/offline-key/verify",
                          json={"company_id": company_id},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["probed"] is False
    assert r.json()["configured"] is True


async def test_diagnostics_counts_vault_key(client, tenants, monkeypatch):
    """密钥只在密钥库里时，「模型自检」不能报"缺密钥"。"""
    alice, company_id = tenants.alice, tenants.alice_co["id"]

    async with AsyncSessionLocal() as db:
        db.add(Model(company_id=company_id, name="自检测试模型",
                     provider_type=PROVIDER, base_url="http://127.0.0.1:9/v1",
                     model_id="diag-model", api_key_env=ENV_NAME, enabled=True))
        await db.commit()

    # 抹掉平台侧取值，只留密钥库这一条路
    monkeypatch.setattr(api_key_resolver, "env_key", lambda *a, **k: "")

    r = await client.get("/models/diagnostics", headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ready"] is False
    assert "缺少密钥" in r.json()["hint"]

    await _store(company_id, ENV_NAME, "vault-diag-key")

    r = await client.get("/models/diagnostics", headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ready"] is True
    assert r.json()["hint"] == ""
