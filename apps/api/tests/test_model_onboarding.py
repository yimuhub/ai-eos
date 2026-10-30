"""安装最后一步「接上大模型」的守卫。

两个新接口解决冷启动断链：
* `POST /models/env-key`：把密钥写进本机 .env 并立即生效，替代"手改文件再重启"；
* `POST /models/{mid}/bind-agents`：把模型批量绑给公司全部员工，
  替代逐个员工点"设为首选模型"。

这里锁住三类最容易退化的性质：权限边界、密钥不外泄、绑定语义（只补未绑定 /
可整体替换）。测试把 .env 重定向到临时目录，绝不碰真实文件。
"""
from __future__ import annotations

import os
import stat

import pytest

from app.db.session import AsyncSessionLocal
from app.models.user import User

pytestmark = pytest.mark.asyncio

ENV_NAME = "OPENAI_COMPAT_API_KEY"
SECRET = "sk-test-abcdef1234567890"
BASE_URL = "http://127.0.0.1:9/v1"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """把 .env 重定向到临时文件，并让 monkeypatch 负责还原进程环境。"""
    path = tmp_path / ".env"
    monkeypatch.setenv("AIEOS_ENV_FILE", str(path))
    # 记录原始状态：接口内部会直接写 os.environ，用例结束后由 monkeypatch 清掉
    monkeypatch.setenv(ENV_NAME, "leftover-from-previous-run")
    return path


async def _promote(user_id: str) -> None:
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user_id)
        assert u is not None
        u.is_superadmin = True
        await db.commit()


async def _make_model(client, user, company_id: str | None, name: str = "接入用模型") -> str:
    body = {
        "name": name,
        "provider_type": "openai_compat",
        "base_url": BASE_URL,
        "api_key_env": ENV_NAME,
        "model_id": "some-model",
    }
    if company_id:
        body["company_id"] = company_id
    r = await client.post("/models", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _make_agents(client, user, company_id: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        r = await client.post("/agents", json={
            "company_id": company_id, "name": f"员工-{i}"},
            headers=user["headers"])
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


# ---------------------------------------------------------------------------
# POST /models/env-key
# ---------------------------------------------------------------------------

async def test_env_key_requires_superadmin(client, make_user, env_file):
    user = await make_user()
    r = await client.post("/models/env-key", json={
        "env_name": ENV_NAME, "api_key": SECRET}, headers=user["headers"])
    assert r.status_code == 403, r.text
    assert not env_file.exists(), "无权限的请求不应写入任何文件"


async def test_env_key_rejects_non_whitelisted_names(client, make_user, env_file):
    user = await make_user()
    await _promote(user["id"])
    for name in ("JWT_SECRET", "DATABASE_URL", "MY_OWN_KEY"):
        r = await client.post("/models/env-key", json={
            "env_name": name, "api_key": SECRET}, headers=user["headers"])
        assert r.status_code == 400, f"{name} 应被拒绝: {r.text}"
    assert not env_file.exists()


@pytest.mark.parametrize("bad_key", [
    "short",                    # 太短
    "sk-with space-123456",     # 空白（多为复制时多选了字符）
    'sk-"quoted"-123456',       # 引号会破坏 .env 语法
    "sk-line\nbreak-123456",    # 换行会注入额外变量
])
async def test_env_key_validates_key_shape(client, make_user, env_file, bad_key):
    user = await make_user()
    await _promote(user["id"])
    r = await client.post("/models/env-key", json={
        "env_name": ENV_NAME, "api_key": bad_key}, headers=user["headers"])
    assert r.status_code == 400, f"{bad_key!r} 应被拒绝: {r.text}"
    assert not env_file.exists()


async def test_env_key_writes_private_file_and_takes_effect(client, make_user,
                                                            env_file):
    """写 .env 必须无损保留其它配置，且不重启就能被自检看到。"""
    env_file.write_text(
        "JWT_SECRET=keep-me\n"
        f"{ENV_NAME}=old-key-value-000\n"
        "OTHER_SETTING=1\n",
        encoding="utf-8")
    user = await make_user()
    await _promote(user["id"])
    company = (await client.post("/companies", json={"name": "密钥公司"},
                                 headers=user["headers"])).json()
    await _make_model(client, user, company["id"])

    r = await client.post("/models/env-key", json={
        "env_name": ENV_NAME, "api_key": SECRET}, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] is True and body["applied"] is True
    assert SECRET not in r.text, "响应里出现了密钥明文"
    assert body["masked"] != SECRET

    content = env_file.read_text(encoding="utf-8")
    assert "JWT_SECRET=keep-me" in content, "写密钥不能毁掉 .env 里其它配置"
    assert "OTHER_SETTING=1" in content
    assert "old-key-value-000" not in content, "同名变量应被替换而不是追加"
    assert content.count(f"{ENV_NAME}=") == 1, "同名变量只能保留一行"
    assert f"{ENV_NAME}={SECRET}" in content

    if os.name == "posix":
        mode = stat.S_IMODE(env_file.stat().st_mode)
        assert mode == 0o600, f".env 权限应为 600，实际 {oct(mode)}"

    assert os.environ[ENV_NAME] == SECRET, "必须同步进程环境，否则要重启才生效"

    r = await client.get("/models/diagnostics", headers=user["headers"])
    d = r.json()
    assert d["ready"] is True, f"写完密钥后自检仍报未就绪：{d}"
    assert SECRET not in r.text


# ---------------------------------------------------------------------------
# POST /models/{mid}/bind-agents
# ---------------------------------------------------------------------------

async def test_bind_agents_covers_whole_company(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    agents = await _make_agents(client, user, co["id"], 3)
    mid = await _make_model(client, user, co["id"])

    r = await client.post(f"/models/{mid}/bind-agents", json={
        "company_id": co["id"]}, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"model_id": mid, "role": "primary", "agent_count": 3,
                        "bound": 3, "skipped": 0}

    for aid in agents:
        r = await client.get(f"/agents/{aid}/models", headers=user["headers"])
        assert {"role": "primary", "model_id": mid} in r.json()

    # 再绑一次：默认只补未绑定，不动已有配置
    r = await client.post(f"/models/{mid}/bind-agents", json={
        "company_id": co["id"]}, headers=user["headers"])
    assert r.json()["bound"] == 0 and r.json()["skipped"] == 3

    # 新员工加入后再绑：只补这个新员工
    extra = await _make_agents(client, user, co["id"], 1)
    r = await client.post(f"/models/{mid}/bind-agents", json={
        "company_id": co["id"]}, headers=user["headers"])
    assert r.json()["bound"] == 1 and r.json()["skipped"] == 3
    r = await client.get(f"/agents/{extra[0]}/models", headers=user["headers"])
    assert {"role": "primary", "model_id": mid} in r.json()


async def test_bind_agents_can_replace_existing_bindings(client, make_user,
                                                         make_company):
    user = await make_user()
    co = await make_company(user)
    agents = await _make_agents(client, user, co["id"], 2)
    first = await _make_model(client, user, co["id"], name="旧模型")
    second = await _make_model(client, user, co["id"], name="新模型")

    await client.post(f"/models/{first}/bind-agents", json={"company_id": co["id"]},
                      headers=user["headers"])
    r = await client.post(f"/models/{second}/bind-agents", json={
        "company_id": co["id"], "only_unbound": False}, headers=user["headers"])
    assert r.json()["bound"] == 2, r.text

    for aid in agents:
        r = await client.get(f"/agents/{aid}/models", headers=user["headers"])
        bindings = r.json()
        assert [b for b in bindings if b["role"] == "primary"] == [
            {"role": "primary", "model_id": second}], "替换后不应残留旧主模型"


async def test_bind_agents_rejects_unowned_and_foreign_models(client, tenants):
    """未归属模型要先认领；别家公司的模型不能绑到自己的员工身上。"""
    alice, alice_co = tenants.alice, tenants.alice_co
    own_model = await _make_model(client, alice, alice_co["id"])
    unowned = await _make_model(client, alice, None, name="无主模型")

    r = await client.post(f"/models/{unowned}/bind-agents", json={
        "company_id": alice_co["id"]}, headers=alice["headers"])
    assert r.status_code == 409, r.text

    # bob 拿 alice 的模型绑定到自己公司 → 归属不一致
    r = await client.post(f"/models/{own_model}/bind-agents", json={
        "company_id": tenants.bob_co["id"]}, headers=tenants.bob["headers"])
    assert r.status_code == 409, r.text

    # bob 连 alice 的公司都不可见 → 404，不泄露公司是否存在
    r = await client.post(f"/models/{own_model}/bind-agents", json={
        "company_id": alice_co["id"]}, headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text


async def test_bind_agents_rejects_bad_role(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    mid = await _make_model(client, user, co["id"])
    r = await client.post(f"/models/{mid}/bind-agents", json={
        "company_id": co["id"], "role": "owner"}, headers=user["headers"])
    assert r.status_code == 400, r.text


async def test_delete_model_detaches_bound_agents(client, make_user,
                                                  make_company):
    """绑定过员工的模型必须能删掉（先解绑），而不是 500 外键错误。

    向导第 4 步会把模型绑给全公司，此后"删除配置"是常见操作：
    外键约束若没人处理，用户只会看到一个 500，配置永远删不掉。
    """
    user = await make_user()
    co = await make_company(user)
    agents = await _make_agents(client, user, co["id"], 2)
    mid = await _make_model(client, user, co["id"])
    await client.post(f"/models/{mid}/bind-agents", json={"company_id": co["id"]},
                      headers=user["headers"])

    r = await client.delete(f"/models/{mid}", headers=user["headers"])
    assert r.status_code == 204, f"删除已绑定的模型失败：{r.text}"

    for aid in agents:
        r = await client.get(f"/agents/{aid}/models", headers=user["headers"])
        assert r.json() == [], "模型删了，绑定记录也必须一起清掉"

    listed = (await client.get("/models", headers=user["headers"])).json()
    assert mid not in [m["id"] for m in listed]


async def test_create_model_defaults_api_key_env(client, make_user, make_company):
    """不填 api_key_env 时落到 provider 默认变量。

    否则模型行"没有指向任何密钥"：.env 里明明配了 Key，调用时取到空串，
    上游报 401 "Token not provided"，用户却以为配置是对的。
    """
    user = await make_user()
    co = await make_company(user)
    r = await client.post("/models", json={
        "name": "没填变量名",
        "provider_type": "openai_compat",
        "base_url": BASE_URL,
        "model_id": "some-model",
        "company_id": co["id"],
    }, headers=user["headers"])
    assert r.status_code == 201, r.text

    from app.models.agent import Model
    async with AsyncSessionLocal() as db:
        m = await db.get(Model, r.json()["id"])
        assert m is not None
        assert m.api_key_env == ENV_NAME
