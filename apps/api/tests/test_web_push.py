"""Web Push（C6）：配置降级、订阅 CRUD、失效清理、通知链路挂接。

测试原则沿用仓库既有约定：真实 ASGI + 真实鉴权 + conftest 临时 SQLite。
pywebpush 是可选依赖，所有用例都在 `_send_sync` 这一层打桩，**不联网**；
密钥用假串，只验证"有没有、传没传"，不验证密码学。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.push_subscription import PushSubscription
from app.services import web_push
from app.services.notification_service import notify

FAKE_PRIVATE = "test-only-vapid-private-key-not-a-real-secret"
FAKE_PUBLIC = "BTestOnlyVapidPublicKey_0123456789abcdef"
ENDPOINT_A = "https://push.example.com/sub/alice-device-a"
ENDPOINT_B = "https://push.example.com/sub/alice-device-b"
ENDPOINT_BOB = "https://push.example.com/sub/bob-device"


@pytest.fixture(autouse=True)
def _clear_vapid(monkeypatch):
    """默认无密钥；需要配置的用例自己 setenv，避免相互污染。"""
    for name in (web_push.ENV_PRIVATE, web_push.ENV_PUBLIC,
                 web_push.ENV_SUBJECT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setenv(web_push.ENV_PRIVATE, FAKE_PRIVATE)
    monkeypatch.setenv(web_push.ENV_PUBLIC, FAKE_PUBLIC)
    return FAKE_PUBLIC


def _subscribe_body(company_id: str, endpoint: str = ENDPOINT_A,
                    p256dh: str = "test-p256dh", auth: str = "test-auth") -> dict:
    return {
        "company_id": company_id,
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
    }


async def _subscribe(client, user: dict, company_id: str,
                     endpoint: str = ENDPOINT_A, **kwargs):
    return await client.post("/notifications/push/subscribe",
                             json=_subscribe_body(company_id, endpoint, **kwargs),
                             headers=user["headers"])


async def _rows() -> list[PushSubscription]:
    async with AsyncSessionLocal() as s:
        return list((await s.execute(select(PushSubscription))).scalars().all())


async def _seed_row(company_id: str, user_id: str, endpoint: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(PushSubscription(company_id=company_id, user_id=user_id,
                               endpoint=endpoint, p256dh="p256dh", auth="auth"))
        await s.commit()


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakePushError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"Push failed: {status_code}")
        self.response = _FakeResponse(status_code)


# ---------------------------------------------------------------- 配置降级

async def test_public_key_unconfigured_returns_200_false(client):
    r = await client.get("/notifications/push/public-key")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is False
    assert body["public_key"] is None
    assert body["reason"] == "not_configured"


async def test_test_push_unconfigured_returns_503(client, make_user):
    user = await make_user()
    r = await client.post("/notifications/push/test", headers=user["headers"])
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "push_not_configured"
    assert body["detail"]


async def test_public_key_configured(client, vapid):
    r = await client.get("/notifications/push/public-key")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["public_key"] == FAKE_PUBLIC
    # 依赖缺失时 available=false，但 configured 仍基于 env
    assert body["available"] is web_push.PYWEBPUSH_AVAILABLE


async def test_dependency_missing_is_reported(client, vapid, make_user,
                                              monkeypatch):
    monkeypatch.setattr(web_push, "PYWEBPUSH_AVAILABLE", False)
    body = (await client.get("/notifications/push/public-key")).json()
    assert body["configured"] is True
    assert body["available"] is False
    assert body["reason"] == "dependency_missing"

    user = await make_user()
    r = await client.post("/notifications/push/test", headers=user["headers"])
    assert r.status_code == 503
    assert r.json()["code"] == "push_not_configured"


# ---------------------------------------------------------------- 订阅 CRUD

async def test_subscribe_is_idempotent_upsert(client, tenants):
    t = tenants
    cid = t.alice_co["id"]

    r1 = await _subscribe(client, t.alice, cid)
    assert r1.status_code == 200, r1.text
    r2 = await _subscribe(client, t.alice, cid, p256dh="rotated", auth="rotated")
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] == r2.json()["id"], "同一 endpoint 不应新增行"

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].user_id == t.alice["id"]
    assert rows[0].company_id == cid
    assert rows[0].p256dh == "rotated"
    assert rows[0].auth == "rotated"


async def test_subscribe_cross_tenant_returns_404(client, tenants):
    t = tenants
    r = await _subscribe(client, t.bob, t.alice_co["id"])
    assert r.status_code == 404, "他人公司必须 404，且不落库"
    assert await _rows() == []


async def test_subscribe_rejects_non_public_endpoint(client, tenants):
    t = tenants
    for endpoint in ("http://push.example.com/x",
                     "https://127.0.0.1/x",
                     "https://localhost/x"):
        r = await _subscribe(client, t.alice, t.alice_co["id"], endpoint)
        assert r.status_code == 400, f"{endpoint} 应被拒绝: {r.text}"
    assert await _rows() == []


async def test_unsubscribe_only_removes_own(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    assert (await _subscribe(client, t.alice, cid, ENDPOINT_A)).status_code == 200
    assert (await _subscribe(client, t.bob, t.bob_co["id"],
                             ENDPOINT_BOB)).status_code == 200

    # alice 拿 bob 的 endpoint 退订：200 但一行都不能少
    r = await client.delete(
        f"/notifications/push/unsubscribe?company_id={cid}&endpoint={ENDPOINT_BOB}",
        headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 0
    assert len(await _rows()) == 2

    # 自己的可以删；重复删仍 200（幂等）
    r = await client.delete(
        f"/notifications/push/unsubscribe?company_id={cid}&endpoint={ENDPOINT_A}",
        headers=t.alice["headers"])
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    r = await client.delete(
        f"/notifications/push/unsubscribe?company_id={cid}&endpoint={ENDPOINT_A}",
        headers=t.alice["headers"])
    assert r.status_code == 200
    assert r.json()["deleted"] == 0

    left = await _rows()
    assert [x.endpoint for x in left] == [ENDPOINT_BOB]


async def test_unsubscribe_accepts_json_body(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    assert (await _subscribe(client, t.alice, cid)).status_code == 200
    r = await client.request(
        "DELETE", "/notifications/push/unsubscribe",
        json={"company_id": cid, "endpoint": ENDPOINT_A},
        headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1


async def test_unsubscribe_cross_tenant_returns_404(client, tenants):
    t = tenants
    assert (await _subscribe(client, t.alice, t.alice_co["id"])).status_code == 200
    r = await client.delete(
        f"/notifications/push/unsubscribe?company_id={t.bob_co['id']}"
        f"&endpoint={ENDPOINT_A}",
        headers=t.alice["headers"])
    assert r.status_code == 404
    assert len(await _rows()) == 1


# ---------------------------------------------------------------- 发送与清理

async def test_send_removes_gone_subscription_and_never_raises(
        client, tenants, vapid, monkeypatch):
    t = tenants
    cid = t.alice_co["id"]
    await _seed_row(cid, t.alice["id"], "https://push.example.com/gone")
    await _seed_row(cid, t.alice["id"], "https://push.example.com/ok")

    calls: list[str] = []

    def fake_send(sub, payload):
        calls.append(sub.endpoint)
        if sub.endpoint.endswith("/gone"):
            raise _FakePushError(410)

    monkeypatch.setattr(web_push, "_send_sync", fake_send)

    async with AsyncSessionLocal() as s:
        result = await web_push.send_to_user(
            s, t.alice["id"], "标题", "正文", url="/m")

    assert result == {"sent": 1, "failed": 1, "removed": 1}
    assert len(calls) == 2
    left = await _rows()
    assert [x.endpoint for x in left] == ["https://push.example.com/ok"]
    assert left[0].last_used_at is not None, "成功发送应记录 last_used_at"


async def test_send_keeps_subscription_on_other_errors(client, tenants, vapid,
                                                       monkeypatch):
    t = tenants
    cid = t.alice_co["id"]
    await _seed_row(cid, t.alice["id"], "https://push.example.com/temp")

    def fake_send(sub, payload):
        raise _FakePushError(503)

    monkeypatch.setattr(web_push, "_send_sync", fake_send)

    async with AsyncSessionLocal() as s:
        result = await web_push.send_to_user(s, t.alice["id"], "标题")
    assert result == {"sent": 0, "failed": 1, "removed": 0}
    assert len(await _rows()) == 1, "临时故障不应解绑设备"


async def test_send_without_config_is_noop(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    await _seed_row(cid, t.alice["id"], ENDPOINT_A)
    async with AsyncSessionLocal() as s:
        result = await web_push.send_to_user(s, t.alice["id"], "标题")
    assert result == {"sent": 0, "failed": 0, "removed": 0}
    assert len(await _rows()) == 1


async def test_test_endpoint_sends_to_current_user(client, tenants, vapid,
                                                   monkeypatch):
    t = tenants
    cid = t.alice_co["id"]
    await _seed_row(cid, t.alice["id"], ENDPOINT_A)
    await _seed_row(cid, t.alice["id"], ENDPOINT_B)
    await _seed_row(t.bob_co["id"], t.bob["id"], ENDPOINT_BOB)

    sent: list[str] = []
    monkeypatch.setattr(
        web_push, "_send_sync",
        lambda sub, payload: sent.append(sub.endpoint))

    r = await client.post("/notifications/push/test", headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": 2, "failed": 0}
    assert set(sent) == {ENDPOINT_A, ENDPOINT_B}, "只能发当前用户自己的设备"


# ---------------------------------------------------------------- 通知链路

async def test_notify_queues_push_only_after_commit(client, tenants, vapid,
                                                    monkeypatch):
    t = tenants
    cid = t.alice_co["id"]
    delivered: list[dict] = []

    async def fake_send(db, user_id, title, body=None, url=None):
        delivered.append({"user_id": user_id, "title": title, "url": url})
        return {"sent": 1, "failed": 0, "removed": 0}

    monkeypatch.setattr(web_push, "send_to_user", fake_send)

    async with AsyncSessionLocal() as s:
        await notify(s, user_id=t.alice["id"], company_id=cid,
                     type="test.event", title="提交前不应发送",
                     body="正文", link="/tasks/1")
        assert web_push.OUTBOX_KEY in s.info, "notify 应只入队，不直接发送"
        assert delivered == []
        await s.commit()

    for _ in range(100):
        if delivered:
            break
        await asyncio.sleep(0.02)
    assert len(delivered) == 1, f"提交后应后台派发一次，实际 {delivered}"
    assert delivered[0]["user_id"] == t.alice["id"]
    assert delivered[0]["url"] == "/tasks/1"


async def test_notify_without_vapid_does_not_queue(client, tenants):
    t = tenants
    async with AsyncSessionLocal() as s:
        await notify(s, user_id=t.alice["id"], company_id=t.alice_co["id"],
                     type="test.event", title="无密钥")
        assert web_push.OUTBOX_KEY not in s.info
        await s.commit()


async def test_notify_rollback_drops_push(client, tenants, vapid,
                                          monkeypatch):
    t = tenants
    delivered: list[dict] = []

    async def fake_send(db, user_id, title, body=None, url=None):
        delivered.append({"title": title})
        return {"sent": 1, "failed": 0, "removed": 0}

    monkeypatch.setattr(web_push, "send_to_user", fake_send)

    async with AsyncSessionLocal() as s:
        await notify(s, user_id=t.alice["id"], company_id=t.alice_co["id"],
                     type="test.event", title="会回滚")
        assert web_push.OUTBOX_KEY in s.info, "回滚前应已入队"
        await s.rollback()

    await asyncio.sleep(0.1)
    assert delivered == [], "业务回滚时推送必须一起丢弃"
    async with AsyncSessionLocal() as s:
        count = (await s.execute(select(func.count()).select_from(
            PushSubscription))).scalar()
    assert count == 0
