"""会话已读跨设备同步（C3）。

重点覆盖三件事：
* 合并只增不减（旧时间戳不会把已读退回未读）；
* 未来时间戳被钳到服务器时间附近（坏时钟不污染）；
* 跨用户 / 跨公司严格隔离（他人公司一律 404）。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.thread_read import ThreadRead

URL = "/thread-reads"


def _ms() -> int:
    return int(time.time() * 1000)


async def _post(client, user: dict, company_id: str, reads: list[dict]):
    return await client.post(URL, json={"company_id": company_id,
                                        "reads": reads},
                             headers=user["headers"])


async def _get(client, user: dict, company_id: str):
    return await client.get(f"{URL}?company_id={company_id}",
                            headers=user["headers"])


async def _rows(user_id: str) -> list[ThreadRead]:
    async with AsyncSessionLocal() as s:
        return list((await s.execute(select(ThreadRead).where(
            ThreadRead.user_id == user_id))).scalars().all())


@pytest.mark.asyncio
async def test_merge_then_read_back(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    now = _ms()
    r = await _post(client, alice, co["id"], [
        {"thread_key": "war:w1", "read_at": now},
        {"thread_key": "single:a1", "read_at": now - 1000},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2
    got = {x["thread_key"]: x["read_at"] for x in body["reads"]}
    assert got == {"war:w1": now, "single:a1": now - 1000}

    r = await _get(client, alice, co["id"])
    assert r.status_code == 200, r.text
    got = {x["thread_key"]: x["read_at"] for x in r.json()["reads"]}
    assert got == {"war:w1": now, "single:a1": now - 1000}


@pytest.mark.asyncio
async def test_merge_is_monotonic(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    now = _ms()
    assert (await _post(client, alice, co["id"], [
        {"thread_key": "war:w1", "read_at": now}])).status_code == 200

    # 更早的时间戳不生效，也不能减少已有值
    r = await _post(client, alice, co["id"], [
        {"thread_key": "war:w1", "read_at": now - 60_000}])
    assert r.status_code == 200
    assert r.json()["updated"] == 0
    assert r.json()["reads"][0]["read_at"] == now

    # 更新的时间戳正常前进
    r = await _post(client, alice, co["id"], [
        {"thread_key": "war:w1", "read_at": now + 1000}])
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    assert r.json()["reads"][0]["read_at"] == now + 1000


@pytest.mark.asyncio
async def test_same_batch_duplicate_keeps_max(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    now = _ms()
    r = await _post(client, alice, co["id"], [
        {"thread_key": "meeting:m1", "read_at": now},
        {"thread_key": "meeting:m1", "read_at": now + 500},
        {"thread_key": "meeting:m1", "read_at": now + 200},
    ])
    assert r.status_code == 200
    assert r.json()["reads"] == [{"thread_key": "meeting:m1",
                                  "read_at": now + 500}]


@pytest.mark.asyncio
async def test_future_timestamp_is_clamped(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    far_future = _ms() + 365 * 24 * 3600 * 1000
    r = await _post(client, alice, co["id"], [
        {"thread_key": "war:w9", "read_at": far_future}])
    assert r.status_code == 200
    stored = r.json()["reads"][0]["read_at"]
    skew = 5 * 60 * 1000
    assert _ms() + skew - 5000 <= stored <= _ms() + skew + 5000


@pytest.mark.asyncio
async def test_foreign_company_is_404(client, tenants):
    # alice 尝试读写 bob 的公司：按仓库约定一律 404，不泄露公司是否存在
    r = await _post(client, tenants.alice, tenants.bob_co["id"], [
        {"thread_key": "war:w1", "read_at": _ms()}])
    assert r.status_code == 404, r.text
    r = await _get(client, tenants.alice, tenants.bob_co["id"])
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_users_are_isolated(client, tenants):
    now = _ms()
    assert (await _post(client, tenants.alice, tenants.alice_co["id"], [
        {"thread_key": "war:shared", "read_at": now}])).status_code == 200
    # bob 在自己的公司读同一 key，互不影响
    r = await _post(client, tenants.bob, tenants.bob_co["id"], [
        {"thread_key": "war:shared", "read_at": now - 5000}])
    assert r.status_code == 200
    assert r.json()["reads"][0]["read_at"] == now - 5000

    alice_rows = await _rows(tenants.alice["id"])
    bob_rows = await _rows(tenants.bob["id"])
    assert len(alice_rows) == 1 and len(bob_rows) == 1
    assert alice_rows[0].company_id == tenants.alice_co["id"]
    assert bob_rows[0].company_id == tenants.bob_co["id"]


@pytest.mark.asyncio
async def test_payload_validation(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await client.get(URL, headers=alice["headers"])
    assert r.status_code == 422  # 缺 company_id

    r = await _post(client, alice, co["id"], [])
    assert r.status_code == 422  # 空批次

    r = await _post(client, alice, co["id"], [
        {"thread_key": "x" * 161, "read_at": _ms()}])
    assert r.status_code == 422  # 超长 key

    r = await _post(client, alice, co["id"], [
        {"thread_key": "war:w1", "read_at": -1}])
    assert r.status_code == 422  # 负时间戳


@pytest.mark.asyncio
async def test_requires_auth(client, tenants):
    r = await client.get(f"{URL}?company_id={tenants.alice_co['id']}")
    assert r.status_code == 401
