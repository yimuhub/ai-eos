"""模型供应商与模型分类。

锁住四条主线：
1. 供应商按公司隔离，不能跨租户读/改/拉取；
2. 拉取结果按上游元数据与模型名识别文本/图片/视频/多模态；
3. 导入按类型生成系统化配置，重复导入幂等，且不覆盖人工配置；
4. 密钥仅超管可写、只回显掩码，写入后子模型同步变量名。
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.model_discovery import classify_model
from app.models.agent import Model
from app.models.user import User

BASE_URL = "http://127.0.0.1:9/v1"
SECRET = "test-provider-key-1234567890"


async def _promote(user_id: str) -> None:
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user_id)
        assert u is not None
        u.is_superadmin = True
        await db.commit()


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _install_fake_models(monkeypatch, payload: dict, status_code: int = 200) -> dict:
    """替换 httpx.AsyncClient：只验证 URL/鉴权头，不发真实请求。"""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured.setdefault("calls", []).append((url, headers or {}))
            return _FakeResp(payload, status_code)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return captured


async def _create_provider(client, user, company_id: str, *,
                           name: str = "测试供应商",
                           api_key_env: str | None = None) -> dict:
    body = {
        "name": name,
        "provider_type": "openai_compat",
        "base_url": BASE_URL,
        "company_id": company_id,
    }
    if api_key_env:
        body["api_key_env"] = api_key_env
    r = await client.post("/model-providers", json=body,
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 分类

def test_classify_model_types_and_multimodal():
    text = classify_model("gpt-4o")
    assert text["model_type"] == "text"
    assert text["multimodal"] is True
    assert {"chat", "vision", "tools"} <= set(text["capabilities"])

    image = classify_model("dall-e-3")
    assert image["model_type"] == "image"
    assert image["capabilities"] == ["image_generation"]

    video = classify_model("sora-2")
    assert video["model_type"] == "video"
    assert video["multimodal"] is True

    emb = classify_model("text-embedding-3-large")
    assert emb["model_type"] == "embedding"
    assert emb["multimodal"] is False

    audio = classify_model("whisper-1")
    assert audio["model_type"] == "audio"


def test_classify_prefers_upstream_modalities():
    """名字看不出来时，以上游声明的输出模态为准。"""
    item = {
        "id": "acme-gen-1",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["image"],
        },
        "context_length": 8192,
    }
    got = classify_model("acme-gen-1", item)
    assert got["model_type"] == "image"
    assert got["multimodal"] is True
    assert got["config"]["sizes"]


def test_default_configs_are_systematized():
    text = classify_model("gpt-4o", {"context_length": 128000,
                                     "max_output_tokens": 16384})
    assert text["config"]["context_window"] == 128000
    # max_tokens 收敛到 8192，避免把上游的超大输出上限直接当默认值
    assert text["config"]["max_tokens"] == 8192
    assert text["config"]["temperature"] == 0.7

    video = classify_model("veo-3")
    cfg = video["config"]
    assert cfg["durations"] and cfg["resolutions"] and cfg["aspect_ratios"]


# ------------------------------------------------------------ 供应商 CRUD

@pytest.mark.asyncio
async def test_provider_tenant_isolation(client, tenants):
    alice, bob = tenants.alice, tenants.bob
    provider = await _create_provider(client, alice, tenants.alice_co["id"])

    mine = await client.get("/model-providers", headers=alice["headers"])
    assert mine.status_code == 200
    assert [p["id"] for p in mine.json()] == [provider["id"]]

    other = await client.get("/model-providers", headers=bob["headers"])
    assert other.status_code == 200 and other.json() == []

    for method, url, body in (
        ("get", f"/model-providers/{provider['id']}", None),
        ("patch", f"/model-providers/{provider['id']}", {"name": "偷改"}),
        ("delete", f"/model-providers/{provider['id']}", None),
        ("post", f"/model-providers/{provider['id']}/pull", {}),
    ):
        r = await getattr(client, method)(url, headers=bob["headers"],
                                          **({"json": body} if body is not None else {}))
        assert r.status_code == 404, f"{method} {url} 应跨租户 404: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_provider_create_validates_url_env_and_name(client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    headers = alice["headers"]

    r = await client.post("/model-providers", json={
        "name": "元数据", "provider_type": "openai_compat",
        "base_url": "http://169.254.169.254/latest/meta-data/",
        "company_id": company_id}, headers=headers)
    assert r.status_code == 400 and "不安全" in r.text

    r = await client.post("/model-providers", json={
        "name": "坏变量", "provider_type": "openai_compat",
        "base_url": BASE_URL, "api_key_env": "JWT_SECRET",
        "company_id": company_id}, headers=headers)
    assert r.status_code == 400

    r = await client.post("/model-providers", json={
        "name": "自定义", "provider_type": "openai_compat",
        "base_url": BASE_URL, "company_id": company_id}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["api_key_env"].startswith("AIEOS_PROVIDER_")
    assert r.json()["api_key_env"].endswith("_API_KEY")

    r = await client.post("/model-providers", json={
        "name": "自定义", "provider_type": "openai_compat",
        "base_url": BASE_URL, "company_id": company_id}, headers=headers)
    assert r.status_code == 409, "同公司重名应被拒绝"


# --------------------------------------------------------- 拉取与导入

@pytest.mark.asyncio
async def test_pull_and_import_classifies_models(client, tenants, monkeypatch):
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    captured = _install_fake_models(monkeypatch, {"data": [
        {"id": "gpt-4o", "context_length": 128000,
         "architecture": {"input_modalities": ["text", "image"],
                          "output_modalities": ["text"]}},
        {"id": "gpt-image-1",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["image"]}},
        {"id": "sora-2",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["video"]}},
    ]})
    r = await client.post(f"/model-providers/{provider['id']}/pull", json={},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    by_id = {m["id"]: m for m in items}
    assert by_id["gpt-4o"]["model_type"] == "text"
    assert by_id["gpt-4o"]["multimodal"] is True
    assert by_id["gpt-image-1"]["model_type"] == "image"
    assert by_id["sora-2"]["model_type"] == "video"
    assert captured["calls"][0][0] == f"{BASE_URL}/models"

    r = await client.post(f"/model-providers/{provider['id']}/import", json={
        "models": [{"model_id": m["id"], "name": m["id"],
                    "model_type": m["model_type"],
                    "multimodal": m["multimodal"],
                    "capabilities": m["capabilities"],
                    "upstream": {"context_window": 128000}}
                   for m in items]},
        headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["imported_count"] == 3

    models = (await client.get("/models",
                               headers=tenants.alice["headers"])).json()
    assert {m["model_id"] for m in models} == {"gpt-4o", "gpt-image-1", "sora-2"}
    text = next(m for m in models if m["model_id"] == "gpt-4o")
    assert text["provider_id"] == provider["id"]
    assert text["model_type"] == "text"
    assert text["multimodal"] is True
    assert text["config"]["context_window"] == 128000
    assert text["enabled"] is True
    assert text["config_source"] == "auto"


@pytest.mark.asyncio
async def test_import_is_idempotent_and_keeps_manual_config(client, tenants,
                                                           monkeypatch):
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    _install_fake_models(monkeypatch, {"data": [{
        "id": "gpt-4o", "context_length": 128000}]})
    pulled = (await client.post(f"/model-providers/{provider['id']}/pull",
                                json={},
                                headers=tenants.alice["headers"])).json()["items"][0]
    item = {"model_id": pulled["id"], "model_type": pulled["model_type"],
            "multimodal": pulled["multimodal"],
            "capabilities": pulled["capabilities"]}

    first = await client.post(f"/model-providers/{provider['id']}/import",
                              json={"models": [item]},
                              headers=tenants.alice["headers"])
    assert first.status_code == 200
    models = (await client.get("/models",
                               headers=tenants.alice["headers"])).json()
    model = models[0]
    assert model["config"]["temperature"] == 0.7

    patched = await client.patch(f"/models/{model['id']}",
                                 json={"config": {"temperature": 0.1}},
                                 headers=tenants.alice["headers"])
    assert patched.status_code == 200, patched.text

    again = await client.post(f"/model-providers/{provider['id']}/import",
                              json={"models": [item]},
                              headers=tenants.alice["headers"])
    assert again.status_code == 200
    assert again.json()["imported_count"] == 1, "同一模型再次导入应更新而非新增"
    models = (await client.get("/models",
                               headers=tenants.alice["headers"])).json()
    assert len(models) == 1
    assert models[0]["config"]["temperature"] == 0.1
    assert models[0]["config_source"] == "manual"

    overwrite = await client.post(f"/model-providers/{provider['id']}/import",
                                  json={"models": [dict(item,
                                                        overwrite_config=True)]},
                                  headers=tenants.alice["headers"])
    assert overwrite.status_code == 200
    models = (await client.get("/models",
                               headers=tenants.alice["headers"])).json()
    assert models[0]["config"]["temperature"] == 0.7
    assert models[0]["config_source"] == "auto"


@pytest.mark.asyncio
async def test_import_adopts_legacy_direct_model(client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    r = await client.post("/models", json={
        "name": "旧版直连", "provider_type": "openai_compat",
        "base_url": BASE_URL, "model_id": "gpt-4o",
        "company_id": company_id}, headers=alice["headers"])
    assert r.status_code == 201
    legacy_id = r.json()["id"]

    provider = await _create_provider(client, alice, company_id)
    _install_fake_models(monkeypatch, {"data": [{"id": "gpt-4o"}]})
    pulled = (await client.post(f"/model-providers/{provider['id']}/pull",
                                json={}, headers=alice["headers"])).json()
    item = pulled["items"][0]
    imported = await client.post(f"/model-providers/{provider['id']}/import",
                                 json={"models": [{"model_id": item["id"],
                                                   "model_type": item["model_type"]}]},
                                 headers=alice["headers"])
    assert imported.status_code == 200, imported.text
    assert imported.json()["adopted"] == ["gpt-4o"]

    models = (await client.get("/models", headers=alice["headers"])).json()
    assert len(models) == 1
    assert models[0]["id"] == legacy_id
    assert models[0]["provider_id"] == provider["id"]


@pytest.mark.asyncio
async def test_provider_update_propagates_runtime_fields(client, tenants,
                                                         monkeypatch):
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    _install_fake_models(monkeypatch, {"data": [{"id": "gpt-4o"}]})
    item = (await client.post(f"/model-providers/{provider['id']}/pull", json={},
                              headers=tenants.alice["headers"])).json()["items"][0]
    await client.post(f"/model-providers/{provider['id']}/import",
                      json={"models": [{"model_id": item["id"]}]},
                      headers=tenants.alice["headers"])

    r = await client.patch(f"/model-providers/{provider['id']}",
                           json={"base_url": "http://127.0.0.1:10/v1"},
                           headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    model = (await client.get("/models",
                              headers=tenants.alice["headers"])).json()[0]
    assert model["base_url"] == "http://127.0.0.1:10/v1"
    # 变量名不是密钥内容，但 GET /models 不暴露它；直接读库确认已随供应商同步
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(Model).where(Model.id == model["id"]))).scalar_one()
    assert row.api_key_env == provider["api_key_env"]


@pytest.mark.asyncio
async def test_type_change_regenerates_default_config(client, tenants, monkeypatch):
    """把文本模型改成图片模型后，配置必须跟着换，不能留着 temperature。"""
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    _install_fake_models(monkeypatch, {"data": [{"id": "gpt-4o"}]})
    item = (await client.post(f"/model-providers/{provider['id']}/pull", json={},
                              headers=tenants.alice["headers"])).json()["items"][0]
    await client.post(f"/model-providers/{provider['id']}/import",
                      json={"models": [{"model_id": item["id"]}]},
                      headers=tenants.alice["headers"])
    model = (await client.get("/models",
                              headers=tenants.alice["headers"])).json()[0]
    assert "temperature" in model["config"]

    r = await client.patch(f"/models/{model['id']}",
                           json={"model_type": "image", "reset_config": True},
                           headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    changed = (await client.get("/models",
                                headers=tenants.alice["headers"])).json()[0]
    assert changed["model_type"] == "image"
    assert changed["config"]["default_size"] == "1024x1024"
    assert "temperature" not in changed["config"]
    assert changed["config_source"] == "auto"


@pytest.mark.asyncio
async def test_delete_provider_requires_empty(client, tenants):
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    r = await client.post("/models", json={
        "name": "子模型", "provider_type": "openai_compat",
        "base_url": BASE_URL, "model_id": "m1",
        "company_id": tenants.alice_co["id"]}, headers=tenants.alice["headers"])
    model_id = r.json()["id"]
    await client.patch(f"/models/{model_id}",
                       json={"provider_id": provider["id"]},
                       headers=tenants.alice["headers"])

    r = await client.delete(f"/model-providers/{provider['id']}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 409

    await client.delete(f"/models/{model_id}",
                        headers=tenants.alice["headers"])
    r = await client.delete(f"/model-providers/{provider['id']}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 204


# --------------------------------------------------------------- 密钥

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("AIEOS_ENV_FILE", str(path))
    return path


@pytest.mark.asyncio
async def test_provider_key_requires_superadmin_and_is_masked(
        client, tenants, env_file, monkeypatch):
    provider = await _create_provider(client, tenants.alice,
                                      tenants.alice_co["id"])
    url = f"/model-providers/{provider['id']}/key"

    r = await client.post(url, json={"api_key": SECRET},
                          headers=tenants.alice["headers"])
    assert r.status_code == 403, r.text
    assert not env_file.exists()

    await _promote(tenants.alice["id"])
    r = await client.post(url, json={"api_key": SECRET},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    assert r.json()["masked"] != SECRET
    env_name = r.json()["env_name"]
    assert env_name == provider["api_key_env"]
    assert f"{env_name}={SECRET}" in env_file.read_text(encoding="utf-8")

    listing = (await client.get("/model-providers",
                                headers=tenants.alice["headers"])).json()
    assert listing[0]["api_key_present"] is True
    assert SECRET not in str(listing), "列表接口回显了密钥明文"
