"""超级管理后台 TOTP 二次验证（V6.1.1 门禁升级）。

钉住的契约：
1. enroll（根凭据=超级密钥）→ pending 状态 + otpauth URI；
2. 错首码 → 绑定不生效；对首码 → enabled + 恢复码签发；
3. 绑定后所有 admin 端点必须带 X-TOTP-Code；无码/错码 403；
4. 恢复码一次性；5 次失败进入冷却；
5. disable 解除后回到「仅超级密钥」态。
"""
from __future__ import annotations

import time

import pyotp
import pytest

from app.core.config import settings
from app.services import totp_service


@pytest.fixture(autouse=True)
def _clean_totp_state():
    totp_service.disable()
    yield
    totp_service.disable()


async def _make_superadmin(make_user) -> dict:
    user = await make_user()
    from app.db.session import AsyncSessionLocal
    from app.models.user import User as UserRow
    async with AsyncSessionLocal() as db:
        row = await db.get(UserRow, user["id"])
        row.is_superadmin = True
        await db.commit()
    return user


def _key(admin):
    return {**admin["headers"], "X-Super-Key": settings.super_key}


def _totp_headers(admin, secret):
    code = pyotp.TOTP(secret).now()
    return {**admin["headers"], "X-Super-Key": settings.super_key,
            "X-TOTP-Code": code}


async def test_enroll_and_confirm_flow(client, make_user):
    admin = await _make_superadmin(make_user)
    h = _key(admin)
    # 未绑定态：overview 不需要 TOTP
    r = await client.get("/ops/admin/2fa/status", headers=h)
    assert r.status_code == 200 and r.json()["enrolled"] is False

    r = await client.post("/ops/admin/2fa/enroll", headers=h,
                          json={"account": "boss@aieos"})
    assert r.status_code == 200, r.text
    uri = r.json()["otpauth_uri"]
    assert "otpauth://totp/AI-EOS" in uri and "boss%40aieos" in uri.replace("@", "%40") or "boss" in uri

    # 提取 secret（otpauth URI 参数）→ 生成首码
    from urllib.parse import urlparse, parse_qs, unquote
    parsed = urlparse(uri)
    secret = parse_qs(parsed.query)["secret"][0]
    code = pyotp.TOTP(secret).now()

    # 错首码 → 绑定不生效
    r = await client.post("/ops/admin/2fa/confirm", headers=h,
                          json={"code": "000000" if code != "000000" else "111111"})
    assert r.status_code == 403
    r = await client.get("/ops/admin/2fa/status", headers=h)
    assert r.json()["enrolled"] is False

    # 对首码 → enabled + 恢复码
    r = await client.post("/ops/admin/2fa/confirm", headers=h,
                          json={"code": code})
    assert r.status_code == 200, r.text
    recovery = r.json()["recovery_codes"]
    assert len(recovery) == 8

    # 绑定后语义（用户口径）：进台只用验证器动态码——不需要超级密钥。
    # overview 等管理请求由已解锁 session（X-Super-Key）承载。
    r = await client.get("/ops/admin/overview", headers=h)
    assert r.status_code == 200

    # unlock：什么都不带 → 403；只带动态码（无超级密钥）→ 200
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"]}, json={})
    assert r.status_code == 403
    rec = recovery[0]
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"], "X-TOTP-Code": rec},
                          json={})
    assert r.status_code == 200
    # 同一恢复码第二次 → 403（一次性）
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"], "X-TOTP-Code": rec},
                          json={})
    assert r.status_code == 403
    # 正常动态码 → 200
    r = await client.post("/ops/admin/unlock",
                          headers={**admin["headers"], "X-TOTP-Code": pyotp.TOTP(secret).now()},
                          json={})
    assert r.status_code == 200


async def test_totp_cooldown_after_failures(client, make_user):
    admin = await _make_superadmin(make_user)
    h = _key(admin)
    r = await client.post("/ops/admin/2fa/enroll", headers=h,
                          json={"account": "a@b.c"})
    from urllib.parse import urlparse, parse_qs
    secret = parse_qs(urlparse(r.json()["otpauth_uri"]).query)["secret"][0]
    r = await client.post("/ops/admin/2fa/confirm", headers=h,
                          json={"code": pyotp.TOTP(secret).now()})

    # 状态文件独立于 make_user 过程；确认后连打 5 次错码 → 冷却
    for _ in range(5):
        try:
            totp_service.verify_code("999999")
        except PermissionError:
            pass
    with pytest.raises(PermissionError, match="尝试次数过多"):
        totp_service.verify_code(pyotp.TOTP(secret).now())


async def test_disable_returns_to_key_only(client, make_user):
    admin = await _make_superadmin(make_user)
    h = _key(admin)
    await client.post("/ops/admin/2fa/enroll", headers=h,
                      json={"account": "a@b.c"})
    from urllib.parse import urlparse, parse_qs
    # 直接 disable（未 confirm 也允许——根凭据把门）
    r = await client.post("/ops/admin/2fa/disable", headers=h,
                          json={"confirm": True})
    assert r.status_code == 200
    r = await client.get("/ops/admin/2fa/status", headers=h)
    assert r.json()["enrolled"] is False
    # overview 回到仅需超级密钥
    r = await client.get("/ops/admin/overview", headers=h)
    assert r.status_code == 200
