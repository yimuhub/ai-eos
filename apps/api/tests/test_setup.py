"""安装向导（/setup）行为护栏。

覆盖点：
* 空库 `needs_setup=true`，安装后变 `false`；
* 安装在一个事务里落齐超管 / 公司 / owner 成员关系 / 事件日志；
* 返回的 token 能直接当登录态用（调 /auth/me）；
* 二次安装 409 且不产生任何新行（用户数、公司数不变）；
* 弱密码 422（与注册同一份规则）；
* 邮箱/用户名冲突与注册一致，给 409 且不留半套数据。

全部走 conftest 的临时 SQLite 与进程内 ASGI 客户端，不碰真实库。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.user import User


@pytest.fixture(autouse=True)
def _isolate_rate_limit(monkeypatch):
    """限流走内存并逐用例清零。

    与 auth 的限流用例同理：若本机恰好跑着 Redis，计数会跨用例累积，
    后面的用例可能莫名 429——测试不能依赖环境。
    """
    from app.api.routes import setup as setup_mod

    async def _no_redis():
        return None

    monkeypatch.setattr(setup_mod, "_rate_redis", _no_redis)
    setup_mod._rate_hits.clear()
    yield
    setup_mod._rate_hits.clear()


def _payload(**overrides) -> dict:
    suffix = uuid.uuid4().hex[:8]
    body = {
        "username": f"admin_{suffix}",
        "email": f"admin_{suffix}@example.com",
        "password": "Setup1234pass",
        "display_name": "平台管理员",
        "company_name": "示例公司",
        "description": "安装向导创建",
    }
    body.update(overrides)
    return body


async def _row_counts() -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        users = (await db.execute(
            select(func.count()).select_from(User))).scalar_one()
        companies = (await db.execute(
            select(func.count()).select_from(Company))).scalar_one()
    return users, companies


async def test_status_on_empty_database_needs_setup(client):
    r = await client.get("/setup/status")
    assert r.status_code == 200, r.text
    assert r.json() == {"needs_setup": True}


async def test_setup_returns_201_with_usable_tokens(client):
    body = _payload()
    r = await client.post("/setup", json=body)
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["token_type"] == "bearer"
    assert data["access_token"] and data["refresh_token"]
    assert data["user"]["username"] == body["username"]
    assert data["user"]["email"] == body["email"]
    assert data["user"]["display_name"] == body["display_name"]
    assert data["user"]["is_superadmin"] is True
    assert data["company"]["name"] == body["company_name"]
    assert data["company"]["id"]

    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["id"] == data["user"]["id"]


async def test_setup_marks_user_as_superadmin(client):
    body = _payload()
    r = await client.post("/setup", json=body)
    assert r.status_code == 201, r.text

    async with AsyncSessionLocal() as db:
        user = (await db.execute(
            select(User).where(User.username == body["username"])
        )).scalar_one()
        assert user.is_superadmin is True
        assert user.is_active is True
        # 库里存的是哈希，不是明文
        assert user.password_hash != body["password"]


async def test_setup_creates_company_owned_by_new_user(client):
    body = _payload()
    r = await client.post("/setup", json=body)
    assert r.status_code == 201, r.text
    data = r.json()

    async with AsyncSessionLocal() as db:
        company = await db.get(Company, data["company"]["id"])
        assert company is not None
        assert company.owner_id == data["user"]["id"]
        assert company.name == body["company_name"]
        assert company.description == body["description"]


async def test_setup_creates_active_owner_membership(client):
    body = _payload()
    r = await client.post("/setup", json=body)
    assert r.status_code == 201, r.text
    data = r.json()

    async with AsyncSessionLocal() as db:
        member = (await db.execute(
            select(CompanyMember).where(
                CompanyMember.company_id == data["company"]["id"],
                CompanyMember.user_id == data["user"]["id"])
        )).scalar_one()
        assert member.role == "owner"
        assert member.status == "active"


async def test_second_setup_is_409_and_changes_nothing(client):
    first = await client.post("/setup", json=_payload())
    assert first.status_code == 201, first.text
    before = await _row_counts()

    # 换一套完全不同的身份再装：必须还是"已初始化"，而不是唯一性冲突
    second = await client.post("/setup", json=_payload())
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "already initialized"
    assert await _row_counts() == before


async def test_weak_password_is_rejected_without_partial_rows(client):
    before = await _row_counts()
    r = await client.post("/setup", json=_payload(password="short1"))
    assert r.status_code == 422, r.text
    r = await client.post("/setup", json=_payload(password="onlyletters"))
    assert r.status_code == 422, r.text
    assert await _row_counts() == before

    # 失败后仍可正常安装（没有把"已初始化"写坏）
    ok = await client.post("/setup", json=_payload())
    assert ok.status_code == 201, ok.text


async def test_status_is_false_after_setup(client):
    r = await client.post("/setup", json=_payload())
    assert r.status_code == 201, r.text

    r = await client.get("/setup/status")
    assert r.status_code == 200, r.text
    assert r.json() == {"needs_setup": False}


async def test_setup_rejects_duplicate_email_or_username(client):
    user = _payload()
    reg = await client.post("/auth/register", json={
        "email": user["email"], "username": user["username"],
        "password": user["password"]})
    assert reg.status_code == 201, reg.text
    before = await _row_counts()

    # 邮箱冲突
    r = await client.post("/setup", json=_payload(email=user["email"]))
    assert r.status_code == 409, r.text
    assert "已被注册" in r.json()["detail"]

    # 用户名冲突
    r = await client.post("/setup", json=_payload(username=user["username"]))
    assert r.status_code == 409, r.text
    assert "已被注册" in r.json()["detail"]

    # 两次冲突都不应创建公司，也不应把普通用户提升成超管
    assert await _row_counts() == before
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(User).where(User.username == user["username"])
        )).scalar_one()
        assert not row.is_superadmin


# ---------------------------------------------------------------------------
# 组织预设：安装前必须可读（否则第 3 步的组织预览是空的）
# ---------------------------------------------------------------------------
async def test_org_presets_are_readable_without_login(client):
    r = await client.get("/org-presets")
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list) and items, "预设列表为空，安装页无法预览"
    pid = items[0]["id"]

    d = await client.get(f"/org-presets/{pid}")
    assert d.status_code == 200, d.text
    assert d.json().get("departments"), "预设详情里没有部门，预览无从展示"


async def test_org_preset_path_traversal_is_rejected(client):
    r = await client.get("/org-presets/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (404, 405), r.text


# ---------------------------------------------------------------------------
# 推倒重建：只允许唯一超管，删干净后回到未初始化
# ---------------------------------------------------------------------------
async def _install(client, **overrides):
    body = _payload(**overrides)
    r = await client.post("/setup", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    return body, data, {"Authorization": f"Bearer {data['access_token']}"}


async def test_rebuild_requires_superadmin(client):
    _, _, headers = await _install(client)
    # 再造一个普通账号：注册接口不授超管
    suffix = uuid.uuid4().hex[:8]
    reg = await client.post("/auth/register", json={
        "email": f"member_{suffix}@example.com",
        "username": f"member_{suffix}",
        "password": "Member1234pass",
    })
    assert reg.status_code in (200, 201), reg.text
    login = await client.post("/auth/login", json={
        "identity": f"member_{suffix}", "password": "Member1234pass"})
    assert login.status_code == 200, login.text
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    r = await client.post("/setup/rebuild", json={"company_name": "示例公司"},
                          headers=member_headers)
    assert r.status_code == 404, r.text


async def test_rebuild_rejects_wrong_company_name(client):
    _, _, headers = await _install(client)
    r = await client.post("/setup/rebuild", json={"company_name": "别家公司"},
                          headers=headers)
    assert r.status_code == 400, r.text
    # 名称不匹配时什么都不能删
    assert (await client.get("/setup/status")).json() == {"needs_setup": False}


async def test_rebuild_wipes_install_and_reopens_wizard(client):
    _, _, headers = await _install(client)
    assert (await client.get("/setup/status")).json() == {"needs_setup": False}

    r = await client.post("/setup/rebuild", json={"company_name": "示例公司"},
                          headers=headers)
    assert r.status_code == 204, r.text

    assert (await client.get("/setup/status")).json() == {"needs_setup": True}
    users, companies = await _row_counts()
    assert (users, companies) == (0, 0), "重建后不能留下账号或公司"

    # 旧 token 已失效
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 401

    # 同一个邮箱可以重新安装
    body, _, _ = await _install(client, email="admin@example.com",
                                username="admin2")
    assert body["email"] == "admin@example.com"
