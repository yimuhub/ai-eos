"""认证与账户安全。

覆盖改造中新增/修复的行为：
- 密码强度校验（原实现接受空串与单字符密码）
- 用户名长度校验
- 登录失败限流（原实现可无限次爆破）
- 账号枚举防护（用户不存在与密码错误返回同一提示）
- 令牌校验（伪造/过期/停用账号）
"""
from __future__ import annotations

import pytest

from tests.conftest import DEFAULT_PASSWORD


# ---------------------------------------------------------------- 注册校验

@pytest.mark.parametrize("password,reason", [
    ("", "空密码"),
    ("a", "单字符"),
    ("abc123", "长度不足 8"),
    ("abcdefgh", "缺少数字"),
    ("12345678", "缺少字母"),
    ("x" * 200, "超过长度上限"),
])
async def test_register_rejects_weak_password(client, password, reason):
    """弱口令必须在**注册入口**就被拒绝，而不是写进库里等出事。"""
    r = await client.post("/auth/register", json={
        "email": "weak@example.com", "username": "weakuser", "password": password})
    assert r.status_code == 422, f"{reason} 竟然通过了：{r.status_code} {r.text}"


@pytest.mark.parametrize("username", ["", "a", "x" * 51])
async def test_register_rejects_bad_username(client, username):
    r = await client.post("/auth/register", json={
        "email": "un@example.com", "username": username,
        "password": DEFAULT_PASSWORD})
    assert r.status_code == 422


async def test_register_rejects_invalid_email(client):
    r = await client.post("/auth/register", json={
        "email": "not-an-email", "username": "validname",
        "password": DEFAULT_PASSWORD})
    assert r.status_code == 422


async def test_register_rejects_duplicate(client, make_user):
    user = await make_user(email="dup@example.com", username="dupuser")
    # 同邮箱
    r = await client.post("/auth/register", json={
        "email": user["email"], "username": "another",
        "password": DEFAULT_PASSWORD})
    assert r.status_code == 409
    # 同用户名
    r = await client.post("/auth/register", json={
        "email": "another@example.com", "username": user["username"],
        "password": DEFAULT_PASSWORD})
    assert r.status_code == 409


async def test_duplicate_message_does_not_reveal_which_field(client, make_user):
    """唯一性冲突提示不能区分"邮箱被占"还是"用户名被占"，否则可用于枚举。"""
    user = await make_user(email="leak@example.com", username="leakuser")
    r1 = await client.post("/auth/register", json={
        "email": user["email"], "username": "fresh1", "password": DEFAULT_PASSWORD})
    r2 = await client.post("/auth/register", json={
        "email": "fresh2@example.com", "username": user["username"],
        "password": DEFAULT_PASSWORD})
    assert r1.json()["detail"] == r2.json()["detail"]


# ---------------------------------------------------------------- 登录

async def test_login_by_email_and_username(client, make_user):
    """用户不该被强迫记住"到底该填邮箱还是用户名"。"""
    user = await make_user(email="both@example.com", username="bothways")
    for identity in (user["email"], user["username"]):
        r = await client.post("/auth/login", json={
            "identity": identity, "password": user["password"]})
        assert r.status_code == 200, f"用 {identity} 登录失败"


async def test_login_wrong_password_rejected(client, make_user):
    user = await make_user()
    r = await client.post("/auth/login", json={
        "identity": user["username"], "password": "WrongPass123"})
    assert r.status_code == 401


async def test_login_unknown_user_same_message_as_wrong_password(client, make_user):
    """账号枚举防护：两种失败必须无法区分。"""
    user = await make_user()
    r_wrong_pw = await client.post("/auth/login", json={
        "identity": user["username"], "password": "WrongPass123"})
    r_no_user = await client.post("/auth/login", json={
        "identity": "ghost_user_xyz", "password": "WrongPass123"})
    assert r_wrong_pw.status_code == r_no_user.status_code == 401
    assert r_wrong_pw.json()["detail"] == r_no_user.json()["detail"]


@pytest.fixture
def memory_rate_limit(monkeypatch):
    """强制限流走内存实现。

    限流在探测到 Redis 可用时会改用 Redis 计数（生产正确），此时内存字典
    恒为空——用例若依赖"本机没跑 Redis"就等于依赖环境，开发机/CI 结果不一致。
    """
    from app.api.routes import auth as auth_mod

    async def _no_redis():
        return None

    monkeypatch.setattr(auth_mod, "_rate_redis", _no_redis)
    return auth_mod


async def test_login_rate_limited_after_repeated_failures(client, make_user,
                                                          memory_rate_limit):
    """限流是防爆破解的最低要求。改造前可以无限次尝试。"""
    from app.api.routes.auth import _MAX_FAILS, _login_failures

    _login_failures.clear()
    user = await make_user()
    codes = []
    for _ in range(_MAX_FAILS + 2):
        r = await client.post("/auth/login", json={
            "identity": user["username"], "password": "WrongPass123"})
        codes.append(r.status_code)
    assert 429 in codes, f"连续失败未触发限流，状态码序列：{codes}"
    # 触发限流后，即使密码正确也应被挡住（窗口内）
    r = await client.post("/auth/login", json={
        "identity": user["username"], "password": user["password"]})
    assert r.status_code == 429
    _login_failures.clear()  # 不污染其它用例


async def test_successful_login_clears_failure_counter(client, make_user,
                                                       memory_rate_limit):
    from app.api.routes.auth import _login_failures

    _login_failures.clear()
    user = await make_user()
    await client.post("/auth/login", json={
        "identity": user["username"], "password": "WrongPass123"})
    assert _login_failures, "失败计数未被记录"
    r = await client.post("/auth/login", json={
        "identity": user["username"], "password": user["password"]})
    assert r.status_code == 200
    assert not _login_failures, "成功登录后失败计数未清空"
    _login_failures.clear()


# ---------------------------------------------------------------- 令牌

async def test_me_requires_token(client):
    assert (await client.get("/auth/me")).status_code == 401


async def test_me_rejects_garbage_token(client):
    r = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_me_rejects_token_for_nonexistent_user(client, make_user):
    """签名有效但用户已被删除：必须 401，不能放行。"""
    from app.core.security import create_access_token

    token = create_access_token("00000000-0000-0000-0000-000000000000")
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_me_rejects_disabled_account(client, make_user):
    """账号被停用后，旧令牌必须立即失效。"""
    from sqlalchemy import select

    from app.core.security import create_access_token
    from app.db.session import AsyncSessionLocal
    from app.models.user import User

    user = await make_user()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(User).where(User.id == user["id"]))).scalar_one()
        row.is_active = False
        await db.commit()

    token = create_access_token(user["id"])
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_me_returns_own_identity(client, make_user):
    user = await make_user()
    r = await client.get("/auth/me", headers=user["headers"])
    assert r.status_code == 200
    assert r.json()["id"] == user["id"]
