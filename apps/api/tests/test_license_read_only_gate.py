"""E1 全局只读门禁：License 过期/失效后所有业务写端点统一 403。

## 密钥从哪来

Ed25519 密钥对在本模块内存中生成，私钥不落盘、不进仓库。签名原文
与生产完全一致（`license_service.canonical_payload`），因此这里验证的
是真实的验签路径，而不是 mock。

## 不碰真实库 / 真实 License

DB 走 conftest 的临时 SQLite；License 通过 `AIEOS_LICENSE` +
`AIEOS_LICENSE_PUBKEY` 注入，`AIEOS_LICENSE_FILE` 指向 `tmp_path`，
保证不会读到仓库里的 `license.json`。

## 断言口径

只读门禁必须"挡住写、放行读、放行恢复路径、放行 CORS 预检"，
并且 **403 响应仍然带 X-Request-ID**（门禁在 request_id 中间件内侧）。
"""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import license_service

PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-12-31T23:59:59Z"
GATE_DETAIL = "License 已过期或无效，系统处于只读模式；请更新 License 后重试"
GATE_CODE = "license_read_only"


# ---------------------------------------------------------------------------
# 夹具与签发工具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def signing_key() -> Ed25519PrivateKey:
    """本进程专用的签发密钥（私钥只在内存里）。"""
    return Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def _isolate_license(monkeypatch, tmp_path):
    """每个用例都从"未配置 License"开始，且文件源指向 tmp。"""
    monkeypatch.delenv(license_service.LICENSE_ENV, raising=False)
    monkeypatch.delenv(license_service.PUBKEY_ENV, raising=False)
    monkeypatch.setenv(license_service.LICENSE_FILE_ENV,
                       str(tmp_path / "license.json"))
    license_service.reset_cache()
    yield
    license_service.reset_cache()


def _pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _sign(payload: dict, key: Ed25519PrivateKey) -> str:
    signature = key.sign(license_service.canonical_payload(payload))
    return json.dumps({"payload": payload,
                       "signature": base64.b64encode(signature).decode()})


@pytest.fixture
def install_license(monkeypatch, signing_key):
    """把一份已签名的 License 装进环境（过期/有效由 `expires_at` 决定）。"""

    def _install(*, expires_at: str = FUTURE, seats: int = 10) -> str:
        raw = _sign({
            "licensee": "门禁测试公司",
            "instance": "gate-test",
            "seats": seats,
            "features": ["usage"],
            "expires_at": expires_at,
            "issued_at": "2026-01-01T00:00:00Z",
            "version": 1,
        }, signing_key)
        monkeypatch.setenv(license_service.LICENSE_ENV, raw)
        monkeypatch.setenv(license_service.PUBKEY_ENV, _pub_b64(signing_key))
        license_service.reset_cache()
        return raw

    return _install


def _is_gate_block(response) -> bool:
    return (response.status_code == 403
            and response.json().get("code") == GATE_CODE)


async def _create_agent(client, tenants, name: str = "员工一"):
    return await client.post(
        "/agents",
        json={"company_id": tenants.alice_co["id"], "name": name},
        headers=tenants.alice["headers"])


# ---------------------------------------------------------------------------
# 1) 过期 License：写被挡、读放行、恢复路径放行
# ---------------------------------------------------------------------------

async def test_expired_license_blocks_business_write_with_request_id(
        client, tenants, install_license):
    """核心缺口回归：过期后 POST /agents 必须 403，且仍可排障。"""
    install_license(expires_at=PAST)

    r = await _create_agent(client, tenants)

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == GATE_CODE
    assert body["detail"] == GATE_DETAIL
    # 门禁必须位于 request_id 中间件内侧：403 也要能 grep 到请求 id
    assert r.headers.get("X-Request-ID")


async def test_expired_license_still_allows_reads(client, tenants,
                                                  install_license):
    install_license(expires_at=PAST)

    r = await client.get("/agents", params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.alice["headers"])

    assert r.status_code == 200, r.text


async def test_expired_license_allows_auth_recovery(client, install_license):
    """重新登录/刷新 token 是恢复路径，不能被门禁挡住。"""
    install_license(expires_at=PAST)

    r = await client.post("/auth/refresh",
                          json={"refresh_token": "definitely-not-a-token"})

    assert not _is_gate_block(r), r.text
    # 无效 refresh_token 的真实语义仍是 401，而不是"只读模式"
    assert r.status_code == 401, r.text


async def test_expired_license_allows_setup_path(client, install_license):
    """安装/重装也是恢复路径：空 body 应是 422，而不是门禁 403。"""
    install_license(expires_at=PAST)

    r = await client.post("/setup", json={})

    assert not _is_gate_block(r), r.text
    assert r.status_code == 422, r.text


async def test_tampered_license_also_blocks_writes(client, tenants,
                                                   install_license,
                                                   monkeypatch):
    """签名不可信与过期同等处理（失败关闭）。"""
    raw = install_license()
    envelope = json.loads(raw)
    envelope["payload"]["seats"] = 999  # 改内容但保留原签名
    monkeypatch.setenv(license_service.LICENSE_ENV, json.dumps(envelope))
    license_service.reset_cache()
    assert license_service.get_state().status == "invalid_signature"

    r = await _create_agent(client, tenants)

    assert r.status_code == 403, r.text
    assert r.json()["code"] == GATE_CODE


# ---------------------------------------------------------------------------
# 2) 门禁不能过度拦截：有效 License / 社区模式照常可写
# ---------------------------------------------------------------------------

async def test_valid_license_does_not_block_writes(client, tenants,
                                                   install_license):
    install_license(expires_at=FUTURE)

    r = await _create_agent(client, tenants)

    assert r.status_code == 201, r.text


async def test_community_mode_does_not_block_writes(client, tenants):
    """没有 License = 社区模式，必须与改造前完全一致。"""
    assert license_service.is_read_only() is False

    r = await _create_agent(client, tenants)

    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# 3) CORS 预检永远放行
# ---------------------------------------------------------------------------

async def test_options_preflight_is_never_gated(client, install_license):
    install_license(expires_at=PAST)

    r = await client.options(
        "/agents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
