"""A3：/api/v1 版本前缀别名。

历史前端继续走无前缀路径；外部集成方用 /api/v1 接入时应命中原路由，
鉴权与租户行为完全一致。这里只验证「同一套端点、两种入口」。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_is_reachable_with_and_without_version_prefix(client):
    plain = await client.get("/health")
    versioned = await client.get("/api/v1/health")
    assert plain.status_code == 200
    assert versioned.status_code == 200
    assert versioned.json() == plain.json()


async def test_authenticated_route_keeps_the_same_semantics(client, tenants):
    alice = tenants.alice
    r = await client.get("/api/v1/auth/me", headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["id"] == alice["id"]

    # 不带 token 仍然 401，说明别名没有绕过鉴权
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


async def test_unknown_versioned_path_is_404(client):
    r = await client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404
    # 前缀本身不是一个可命中的业务页（根路径没有路由）
    r = await client.get("/api/v1")
    assert r.status_code == 404
