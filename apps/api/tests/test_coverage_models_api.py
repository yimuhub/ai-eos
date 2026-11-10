"""覆盖率补齐：模型注册 CRUD/归属认领/诊断/测试/批量绑定/供应商探测。

网络全部 mock：供应商探测替换 httpx.AsyncClient，连通性测试替换
PROVIDERS 里的 provider 类；不产生任何真实外发请求。
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import select, update

from app.api.routes import models_api as mr
from app.db.session import AsyncSessionLocal
from app.model_gateway import PROVIDERS
from app.models.agent import Agent, AgentModelConfig, Model, ModelProviderType
from app.models.user import User

pytestmark = pytest.mark.asyncio

VALID_URL = "http://127.0.0.1:11434/v1"


def _body(name: str = "本地模型", company_id: str | None = None, **extra) -> dict:
    body = {"name": name, "provider_type": "openai_compat",
            "model_id": f"m-{uuid.uuid4().hex[:5]}", "base_url": VALID_URL}
    if company_id:
        body["company_id"] = company_id
    body.update(extra)
    return body


async def _create(client, user, company_id: str | None = None, **extra) -> dict:
    r = await client.post("/models", json=_body(company_id=company_id, **extra),
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _unowned_model(company_id: str | None = None, **extra) -> str:
    mid = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        s.add(Model(id=mid, company_id=company_id, name="遗留模型",
                    provider_type=ModelProviderType.OPENAI_COMPAT,
                    base_url=VALID_URL, api_key_env="OPENAI_COMPAT_API_KEY",
                    model_id="legacy", config=extra.get("config")))
        await s.commit()
    return mid


async def test_create_list_update_delete_and_ownership(client, tenants):
    alice, bob = tenants.alice, tenants.bob
    co = tenants.alice_co
    h = alice["headers"]

    # 未归属：任何登录用户都能登记，但列表里明确标记
    m = await _create(client, alice)
    assert m["company_id"] is None
    async with AsyncSessionLocal() as s:
        row = await s.get(Model, m["id"])
        assert row.api_key_env == "OPENAI_COMPAT_API_KEY"  # 自动落默认变量名
    r = await client.get("/models", headers=h)
    assert r.status_code == 200
    assert any(x["id"] == m["id"] and x["unowned"] for x in r.json())

    # 自有公司：只看到自家 + 未归属，看不到别人家
    mine = await _create(client, alice, co["id"],
                         config={"api_key": "sk-leak", "keep": "ok",
                                 "headers": {"Authorization": "Bearer x"}})
    bob_model = await _create(client, bob, tenants.bob_co["id"])
    r = await client.get(f"/models?company_id={co['id']}", headers=h)
    ids = {x["id"] for x in r.json()}
    assert mine["id"] in ids and m["id"] in ids and bob_model["id"] not in ids
    row = next(x for x in r.json() if x["id"] == mine["id"])
    assert "api_key" not in row["config"] and row["config"]["keep"] == "ok"
    assert "Authorization" not in row["config"]["headers"]
    # 指定别家公司 → 404（借参数绕过隔离）
    r = await client.get(f"/models?company_id={tenants.bob_co['id']}", headers=h)
    assert r.status_code == 404

    # 创建校验
    r = await client.post("/models", json=_body(provider_type="nope"), headers=h)
    assert r.status_code == 400
    r = await client.post("/models", json=_body(api_key_env="JWT_SECRET"), headers=h)
    assert r.status_code == 400
    r = await client.post("/models", json=_body(base_url="http://169.254.169.254/x"),
                         headers=h)
    assert r.status_code == 400
    r = await client.post("/models", json=_body(company_id=tenants.bob_co["id"]),
                         headers=h)
    assert r.status_code == 404

    # 未归属配置不能直接改/删，只能认领
    r = await client.patch(f"/models/{m['id']}", json={"name": "改名"}, headers=h)
    assert r.status_code == 409
    r = await client.delete(f"/models/{m['id']}", headers=h)
    assert r.status_code == 409
    r = await client.patch(f"/models/{m['id']}", json={"company_id": co["id"]},
                           headers=h)
    assert r.status_code == 200 and r.json()["claimed"] is True

    # 认领后可正常修改；清空环境变量名会回落到默认变量
    r = await client.patch(f"/models/{m['id']}", json={
        "name": "已认领", "api_key_env": "  ", "provider_type": "anthropic"},
        headers=h)
    assert r.status_code == 200 and r.json()["name"] == "已认领"
    async with AsyncSessionLocal() as s:
        row = await s.get(Model, m["id"])
        assert row.api_key_env == "ANTHROPIC_API_KEY"
    # 转让到自己另一家公司
    co2 = await _company(client, alice)
    r = await client.patch(f"/models/{m['id']}", json={"company_id": co2},
                           headers=h)
    assert r.status_code == 200 and r.json()["company_id"] == co2
    # 转给没有权限的公司 → 404
    r = await client.patch(f"/models/{m['id']}",
                           json={"company_id": tenants.bob_co["id"]}, headers=h)
    assert r.status_code == 404

    # 删除：解绑员工引用后 204；不存在 404
    async with AsyncSessionLocal() as s:
        aid = (await _new_agent(s, co["id"]))
    r = await client.post(f"/agents/{aid}/models", json={
        "role": "primary", "model_id": mine["id"]}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.delete(f"/models/{mine['id']}", headers=h)
    assert r.status_code == 204
    async with AsyncSessionLocal() as s:
        binds = (await s.execute(select(AgentModelConfig).where(
            AgentModelConfig.model_id == mine["id"]))).scalars().all()
    assert binds == []
    r = await client.delete(f"/models/{uuid.uuid4().hex}", headers=h)
    assert r.status_code == 404


async def _company(client, user) -> str:
    r = await client.post("/companies", json={"name": f"公司-{uuid.uuid4().hex[:5]}"},
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _new_agent(session, company_id: str) -> str:
    aid = uuid.uuid4().hex
    session.add(Agent(id=aid, company_id=company_id,
                      name=f"员工-{uuid.uuid4().hex[:5]}"))
    await session.commit()
    return aid


async def test_bind_agents_role_and_scope(client, tenants):
    alice = tenants.alice
    co = tenants.alice_co
    h = alice["headers"]
    model = await _create(client, alice, co["id"])
    async with AsyncSessionLocal() as s:
        a1 = await _new_agent(s, co["id"])
        a2 = await _new_agent(s, co["id"])

    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["bound"] == 2 and r.json()["agent_count"] == 2

    # 默认只补未绑定 → 二次全部跳过
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.json()["bound"] == 0 and r.json()["skipped"] == 2
    # only_unbound=False 覆盖已有绑定
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"], "role": "fallback", "only_unbound": False},
        headers=h)
    assert r.json()["bound"] == 2
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id.in_([a1, a2])))).scalars().all()
    assert {x.role for x in rows} == {"primary", "fallback"}

    # 校验分支
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"], "role": "boss"}, headers=h)
    assert r.status_code == 400
    r = await client.post(f"/models/{uuid.uuid4().hex}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 404
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": tenants.bob_co["id"]}, headers=h)
    assert r.status_code == 404

    unowned = await _unowned_model()
    r = await client.post(f"/models/{unowned}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 409
    # 模型不属于目标公司 → 409
    other_co = await _company(client, alice)
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": other_co}, headers=h)
    assert r.status_code == 409
    # 目标公司没有员工 → 计数全 0
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 200


async def test_diagnostics_and_env_key(client, make_user, make_company,
                                       monkeypatch, tmp_path):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    # 空系统：提示先配模型
    r = await client.get("/models/diagnostics", headers=h)
    assert r.status_code == 200 and r.json()["ready"] is False
    assert "还没有配置任何模型" in r.json()["hint"]

    # 有模型但没密钥：提示缺密钥
    m = await _create(client, user, co["id"])
    r = await client.get("/models/diagnostics", headers=h)
    d = r.json()
    assert d["ready"] is False and "缺少密钥" in d["hint"]
    openai = next(p for p in d["providers"] if p["provider_type"] == "openai_compat")
    assert openai["model_count"] == 1 and openai["key_present"] is False

    # 密钥就位 → ready
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-test-12345678")
    r = await client.get("/models/diagnostics", headers=h)
    d = r.json()
    assert d["ready"] is True and m["id"] in d["usable_model_ids"]
    assert d["hint"] == ""

    # 测试明确失败 → 不再算就绪，提示区分"配了但不通"
    r = await client.patch(f"/models/{m['id']}", json={
        "config": {"last_test_ok": False, "last_test_error": "401 invalid"}},
        headers=h)
    assert r.status_code == 200
    r = await client.get("/models/diagnostics", headers=h)
    d = r.json()
    assert d["ready"] is False and "连通性测试失败" in d["hint"]
    assert "401 invalid" in d["hint"]

    # env-key：仅超管可写；变量白名单/长度/字符校验
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "sk-abcdefgh"},
        headers=h)
    assert r.status_code == 403
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == user["id"]).values(
            is_superadmin=True))
        await s.commit()

    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\nOPENAI_COMPAT_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setenv("AIEOS_ENV_FILE", str(env_path))
    r = await client.post("/models/env-key", json={
        "env_name": "JWT_SECRET", "api_key": "sk-abcdefgh"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "short"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "sk-abc defgh"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "sk-newkey-123456"},
        headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and "…" in r.json()["masked"]
    text = env_path.read_text(encoding="utf-8")
    assert "EXISTING=1" in text and text.count("OPENAI_COMPAT_API_KEY=") == 1
    assert "sk-newkey-123456" in text


async def test_test_model_success_failure_and_guards(client, make_user,
                                                     make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    model = await _create(client, user, co["id"])
    calls = []

    class _OkProvider:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def chat(self, messages, config=None):
            assert config["max_tokens"] == 64
            return {"content": "OK", "raw_model": "fake-1"}

    monkeypatch.setitem(PROVIDERS, ModelProviderType.OPENAI_COMPAT, _OkProvider)
    r = await client.post(f"/models/{model['id']}/test", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["reply"] == "OK"
    async with AsyncSessionLocal() as s:
        row = await s.get(Model, model["id"])
    assert row.config["last_test_ok"] is True and row.config["last_test_at"]

    class _BadProvider:
        def __init__(self, **kwargs):
            pass

        async def chat(self, messages, config=None):
            raise RuntimeError("上游 500")

    monkeypatch.setitem(PROVIDERS, ModelProviderType.OPENAI_COMPAT, _BadProvider)
    r = await client.post(f"/models/{model['id']}/test", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "error"
    async with AsyncSessionLocal() as s:
        row = await s.get(Model, model["id"])
    assert row.config["last_test_ok"] is False
    assert "RuntimeError" in row.config["last_test_error"]

    # 不存在 → 404
    r = await client.post(f"/models/{uuid.uuid4().hex}/test", headers=h)
    assert r.status_code == 404

    # 历史遗留的敏感变量名 / 云元数据地址：测试前拦截
    async with AsyncSessionLocal() as s:
        bad = Model(id=uuid.uuid4().hex, company_id=co["id"], name="坏配置",
                    provider_type=ModelProviderType.OPENAI_COMPAT,
                    base_url=VALID_URL, api_key_env="JWT_SECRET", model_id="x")
        s.add(bad)
        await s.commit()
    r = await client.post(f"/models/{bad.id}/test", headers=h)
    assert r.status_code == 400
    async with AsyncSessionLocal() as s:
        await s.execute(update(Model).where(Model.id == bad.id).values(
            api_key_env="OPENAI_COMPAT_API_KEY",
            base_url="http://169.254.169.254/latest"))
        await s.commit()
    r = await client.post(f"/models/{bad.id}/test", headers=h)
    assert r.status_code == 400


async def test_provider_model_discovery(client, make_user, monkeypatch):
    user = await make_user()
    h = user["headers"]
    state = {"status": 200, "body": {"data": [{"id": "b"}, {"id": "a"}, {}]},
             "error": None, "headers": None}

    class _Resp:
        def __init__(self):
            self.status_code = state["status"]
            self.body = state["body"]

        def json(self):
            return self.body

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            state["headers"] = headers
            if state["error"]:
                raise state["error"]
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    # api_key_env 一律拒绝（历史密钥外发漏洞）
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL, "api_key_env": "JWT_SECRET"}, headers=h)
    assert r.status_code == 400
    # provider 非法 / 元数据地址
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL, "provider_type": "nope"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://169.254.169.254/latest"}, headers=h)
    assert r.status_code == 400

    # 成功（Bearer + /v1/models 归一化 + 排序去空）
    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://127.0.0.1:11434", "api_key": "sk-x"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["a", "b"]
    assert state["headers"]["Authorization"] == "Bearer sk-x"

    # Anthropic 走 x-api-key
    r = await client.post("/models/providers/list-models", json={
        "base_url": "http://127.0.0.1:11434/v1/", "provider_type": "anthropic",
        "api_key": "sk-y"}, headers=h)
    assert r.status_code == 200
    assert state["headers"]["x-api-key"] == "sk-y"

    # 401 / 非 200 不回显上游 body
    state["status"] = 401
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL}, headers=h)
    assert r.json()["error"].startswith("认证失败")
    state["status"] = 500
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL}, headers=h)
    assert "HTTP 500" in r.json()["error"]

    state["error"] = httpx.ConnectError("refused")
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL}, headers=h)
    assert r.json()["error"] == "无法连接该 base_url"
    state["error"] = ValueError("boom")
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL}, headers=h)
    assert "ValueError" in r.json()["error"]


async def test_batch_import_and_convert_config(client, make_user, make_company,
                                               monkeypatch):
    """更新到修复后的契约（B-099/B-100）：
    - batch-import 必须带 company_id 且需要该公司 MANAGE；落库挂公司名下
    - convert-config 是进程级全局设置 → 仅超管可写，任何登录用户可读
    """
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    async def _set_superadmin(flag: bool):
        async with AsyncSessionLocal() as s:
            row = await s.get(User, user["id"])
            row.is_superadmin = flag
            await s.commit()

    # 非超管且无该公司 → 404（不泄露公司存在性）
    r = await client.post("/models/providers/batch-import", json={
        "company_id": co["id"], "name_prefix": "Hermes", "base_url": VALID_URL,
        "model_ids": ["m1", "m2"]}, headers=h)
    # make_company 的 owner 具备 MANAGE → 通过
    assert r.status_code == 200, r.text
    assert r.json()["imported_count"] == 2
    # 落库挂在该公司名下（不再 company_id=None 全平台可见）
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Model).where(
            Model.name.like("Hermes-%")))).scalars().all()
        assert rows and all(m.company_id == co["id"] for m in rows), \
            [(m.name, m.company_id) for m in rows]
    # 再次导入同 provider+model（同公司）→ 跳过
    r = await client.post("/models/providers/batch-import", json={
        "company_id": co["id"], "name_prefix": "Hermes", "base_url": VALID_URL,
        "model_ids": ["m1", "m3"]}, headers=h)
    assert r.json()["imported"] == ["m3"]
    assert r.json()["skipped_existing"] == ["m1"]

    r = await client.post("/models/providers/batch-import", json={
        "company_id": co["id"], "name_prefix": "x", "provider_type": "nope",
        "base_url": VALID_URL, "model_ids": []}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/providers/batch-import", json={
        "company_id": co["id"], "name_prefix": "x", "base_url": VALID_URL,
        "api_key_env": "JWT_SECRET", "model_ids": []}, headers=h)
    assert r.status_code == 400
    r = await client.post("/models/providers/batch-import", json={
        "company_id": co["id"], "name_prefix": "x",
        "base_url": "http://169.254.169.254/x", "model_ids": []}, headers=h)
    assert r.status_code == 400

    # convert-config：读任何登录用户可以；写仅超管（B-100）
    r = await client.get("/models/convert-config", headers=h)
    assert r.status_code == 200 and "convert_model_id" in r.json()
    r = await client.post("/models/convert-config", json={
        "convert_model_id": "cheap-model"}, headers=h)
    assert r.status_code == 403
    await _set_superadmin(True)
    r = await client.post("/models/convert-config", json={
        "convert_model_id": "cheap-model"}, headers=h)
    assert r.status_code == 200 and r.json()["convert_model_id"] == "cheap-model"
    await _set_superadmin(False)
    r = await client.get("/models/convert-config", headers=h)
    assert r.json()["convert_model_id"] == "cheap-model"


async def test_model_config_cleanup_and_superadmin_visibility(client, make_user,
                                                             make_company):
    """缺失分支：无 base_url、嵌套 config 脱敏、超管全量可见、更新校验。"""
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    bob = await make_user()
    bob_co = await make_company(bob)
    bob_model = await _create(client, bob, bob_co["id"], name="别家模型")

    # 不填 base_url 时跳过出站校验，env 落 provider 默认值
    r = await client.post("/models", json={
        "name": "无地址模型", "provider_type": "gemini",
        "model_id": "gemini-pro"}, headers=h)
    assert r.status_code == 201, r.text
    async with AsyncSessionLocal() as s:
        row = await s.get(Model, r.json()["id"])
        assert row.base_url is None
        assert row.api_key_env == "GEMINI_API_KEY"

    model = await _create(client, user, co["id"])

    # 嵌套 config（列表 + dict）同样脱敏；*_env 是变量名引用，保留
    r = await client.patch(f"/models/{model['id']}", json={"config": {
        "items": [{"api_key": "sk-leak", "inner_token_env": "INNER_KEY"},
                  {"keep": 1}],
        "nested": {"client_secret": "s", "authorization": "Bearer z",
                   "keep": "ok"}}}, headers=h)
    assert r.status_code == 200, r.text
    rows = (await client.get(f"/models?company_id={co['id']}", headers=h)).json()
    cfg = next(x for x in rows if x["id"] == model["id"])["config"]
    assert cfg["items"] == [{"inner_token_env": "INNER_KEY"}, {"keep": 1}]
    assert cfg["nested"] == {"keep": "ok"}

    # 更新不存在 → 404；更新时 base_url 仍要过出站闸门且不落库
    r = await client.patch(f"/models/{uuid.uuid4().hex}", json={"name": "x"},
                           headers=h)
    assert r.status_code == 404
    r = await client.patch(f"/models/{model['id']}", json={
        "base_url": "http://169.254.169.254/latest"}, headers=h)
    assert r.status_code == 400
    async with AsyncSessionLocal() as s:
        assert (await s.get(Model, model["id"])).base_url == VALID_URL

    # 超管：不带 company_id 也能看到所有公司的模型
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == user["id"]).values(
            is_superadmin=True))
        await s.commit()
    rows = (await client.get("/models", headers=h)).json()
    ids = {x["id"] for x in rows}
    assert model["id"] in ids and bob_model["id"] in ids


async def test_bind_agents_zero_agents_and_rebind(client, make_user,
                                                  make_company):
    """缺失分支：目标公司无员工、同角色覆盖绑定（only_unbound=False）。"""
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]
    model = await _create(client, user, co["id"])

    # 公司里还没有员工 → 计数全 0，而不是报错
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["agent_count"] == 0
    assert r.json()["bound"] == 0 and r.json()["skipped"] == 0

    async with AsyncSessionLocal() as s:
        aid = await _new_agent(s, co["id"])

    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["bound"] == 1 and r.json()["skipped"] == 0
    # 默认只补未绑定
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"]}, headers=h)
    assert r.json()["bound"] == 0 and r.json()["skipped"] == 1
    # 覆盖已有同角色绑定，不产生重复行
    r = await client.post(f"/models/{model['id']}/bind-agents", json={
        "company_id": co["id"], "only_unbound": False}, headers=h)
    assert r.status_code == 200 and r.json()["bound"] == 1
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == aid))).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "primary" and rows[0].model_id == model["id"]


async def test_provider_list_models_runtime_outbound_rejected(client,
                                                              make_user,
                                                              monkeypatch):
    """请求阶段被出站闸门拒绝（如重定向到内网）也必须转 400，不能 500。"""
    from app.core.net_guard import OutboundURLRejected

    user = await make_user()
    h = user["headers"]

    class _RejectingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            raise OutboundURLRejected("重定向到云元数据地址")

    monkeypatch.setattr(httpx, "AsyncClient", _RejectingClient)
    r = await client.post("/models/providers/list-models", json={
        "base_url": VALID_URL}, headers=h)
    assert r.status_code == 400
    assert "base_url 不安全" in r.json()["detail"]


async def test_env_key_mask_append_defense_and_io_failure(client, make_user,
                                                          monkeypatch, tmp_path):
    """缺失分支：短密钥掩码、export 行/追加写入、纵深防御、写盘失败 500。"""
    user = await make_user()
    h = user["headers"]
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == user["id"]).values(
            is_superadmin=True))
        await s.commit()

    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\nexport ANTHROPIC_API_KEY=old\n",
                        encoding="utf-8")
    monkeypatch.setenv("AIEOS_ENV_FILE", str(env_path))

    # 8 位密钥走短掩码；文件里没有同名行 → 追加而不是覆盖
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "abcdefgh"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["masked"] == "…gh" and r.json()["applied"] is True
    text = env_path.read_text(encoding="utf-8")
    assert "EXISTING=1" in text
    assert "export ANTHROPIC_API_KEY=old" in text  # 其它行原样保留
    assert text.count("OPENAI_COMPAT_API_KEY=") == 1
    assert text.strip().endswith("OPENAI_COMPAT_API_KEY=abcdefgh")
    os.environ.pop("OPENAI_COMPAT_API_KEY", None)  # 不污染后续用例

    # 纵深防御：即使变量名在白名单里，敏感判定命中也要拒绝
    original = mr.is_sensitive_env_name
    monkeypatch.setattr(mr, "is_sensitive_env_name", lambda name: True)
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "sk-12345678"},
        headers=h)
    assert r.status_code == 400
    assert "敏感" in r.json()["detail"]
    monkeypatch.setattr(mr, "is_sensitive_env_name", original)

    # 原子替换失败 → 500 且说明原因；临时文件必须被清理，不留残骸
    def _boom(src, dst):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "replace", _boom)
    r = await client.post("/models/env-key", json={
        "env_name": "OPENAI_COMPAT_API_KEY", "api_key": "sk-12345678"},
        headers=h)
    assert r.status_code == 500
    assert "I/O error" in r.json()["detail"]
    assert list(tmp_path.glob(".env.*.tmp")) == []
