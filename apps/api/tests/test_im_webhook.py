"""C4 IM webhook：四类 payload、加签、提交后派发、回滚丢弃与 SSRF 防护。

全部离线：不发真实请求（monkeypatch `_send_one` 或 `httpx.AsyncClient`）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.services import im_webhook

pytestmark = pytest.mark.asyncio

HOOK = "https://hooks.example.com/open-apis/bot/v2/hook/test-token"
TS = 1700000000


@pytest.fixture
def calls(monkeypatch):
    """记录派发调用，替代真实 HTTP。"""
    recorded: list[tuple[str, dict]] = []

    async def _fake_send(url: str, payload: dict) -> None:
        recorded.append((url, payload))

    monkeypatch.setattr(im_webhook, "_send_one", _fake_send)
    return recorded


def _use_env(monkeypatch, url: str = HOOK, kind: str = "generic",
             secret: str | None = None) -> None:
    monkeypatch.setenv(im_webhook.ENV_URL, url)
    monkeypatch.setenv(im_webhook.ENV_TYPE, kind)
    if secret:
        monkeypatch.setenv(im_webhook.ENV_SECRET, secret)
    else:
        monkeypatch.delenv(im_webhook.ENV_SECRET, raising=False)


# ---------------------------------------------------------------- payload

@pytest.mark.parametrize("channel", ["generic", "slack", "feishu", "dingtalk"])
async def test_build_payload_shapes(monkeypatch, channel):
    _use_env(monkeypatch, kind=channel)
    url, body = im_webhook.build_payload("task.completed", "标题", "正文",
                                         "/tasks/1", timestamp=TS)
    assert url == HOOK
    if channel == "generic":
        assert body == {"type": "task.completed", "title": "标题",
                        "body": "正文", "link": "/tasks/1"}
    elif channel == "slack":
        assert body == {"text": "【标题】正文\n/tasks/1"}
    elif channel == "feishu":
        assert body == {"msg_type": "text",
                        "content": {"text": "【标题】正文\n/tasks/1"}}
        assert "sign" not in body and "timestamp" not in body
    else:
        assert body == {"msgtype": "text",
                        "text": {"content": "【标题】正文\n/tasks/1"}}
        assert "timestamp" not in url


async def test_feishu_signature(monkeypatch):
    _use_env(monkeypatch, kind="feishu", secret="feishu-secret")
    _, body = im_webhook.build_payload("task.completed", "标题", "正文", None,
                                       timestamp=TS)
    expected = base64.b64encode(hmac.new(
        f"{TS}\nfeishu-secret".encode(), b"", hashlib.sha256).digest()
    ).decode()
    assert body["timestamp"] == str(TS)
    assert body["sign"] == expected


async def test_dingtalk_signature_in_query(monkeypatch):
    _use_env(monkeypatch, kind="dingtalk", secret="ding-secret")
    url, body = im_webhook.build_payload("task.completed", "标题", "正文",
                                         None, timestamp=TS)
    assert body["msgtype"] == "text"
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == HOOK
    qs = parse_qs(parsed.query)
    assert qs["timestamp"] == [str(TS)]
    expected = base64.b64encode(hmac.new(
        b"ding-secret", f"{TS}\nding-secret".encode(), hashlib.sha256).digest()
    ).decode()
    assert qs["sign"] == [expected]


# ---------------------------------------------------------------- SSRF

@pytest.mark.parametrize("bad_url", [
    "http://localhost/hook",
    "http://127.0.0.1/hook",
    "http://127.1.2.3/hook",
    "http://0.0.0.0/hook",
    "http://[::1]/hook",
    "http://169.254.169.254/latest/meta-data",
    "http://100.100.100.200/hook",
    "http://10.0.0.8/hook",
    "http://192.168.1.5/hook",
    "http://metadata.google.internal/hook",
    "http://example/hook",
    "ftp://example.com/hook",
    "not a url",
])
async def test_rejects_local_and_internal_urls(monkeypatch, bad_url):
    _use_env(monkeypatch, url=bad_url)
    with pytest.raises(ValueError):
        im_webhook.build_payload("generic", "标题", "正文", None)


async def test_accepts_public_https(monkeypatch):
    _use_env(monkeypatch, url="https://webhooks.example.org/feishu/x")
    url, _ = im_webhook.build_payload("generic", "t", "b", None)
    assert url.startswith("https://webhooks.example.org/")


# ---------------------------------------------------------------- 事务边界

async def test_no_url_is_noop(monkeypatch, calls):
    monkeypatch.delenv(im_webhook.ENV_URL, raising=False)
    async with AsyncSessionLocal() as s:
        assert im_webhook.queue_notification(
            s, kind="task.completed", title="t", body="b", link="/x") is False
        assert im_webhook.OUTBOX_KEY not in s.info
        await s.commit()
    await asyncio.sleep(0.05)
    assert calls == []


async def test_rollback_discards_outbox(monkeypatch, calls):
    _use_env(monkeypatch)
    async with AsyncSessionLocal() as s:
        await s.execute(select(1))  # 开启真实事务，rollback 才会触发事件
        assert im_webhook.queue_notification(
            s, kind="task.completed", title="t", body="b", link="/x") is True
        assert im_webhook.OUTBOX_KEY in s.info
        await s.rollback()
        assert im_webhook.OUTBOX_KEY not in s.info, "回滚必须清空 outbox"
    await asyncio.sleep(0.05)
    assert calls == []


async def test_commit_dispatches(monkeypatch, calls):
    _use_env(monkeypatch)
    async with AsyncSessionLocal() as s:
        im_webhook.queue_notification(
            s, kind="task.failed", title="任务失败", body="原因", link="/tasks/1")
        assert calls == [], "提交前不得外发"
        await s.commit()
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == HOOK
    assert payload == {"type": "task.failed", "title": "任务失败",
                       "body": "原因", "link": "/tasks/1"}


async def test_invalid_configured_url_never_queues(monkeypatch, calls):
    _use_env(monkeypatch, url="http://169.254.169.254/hook")
    async with AsyncSessionLocal() as s:
        assert im_webhook.queue_notification(
            s, kind="task.completed", title="t") is False
        assert im_webhook.OUTBOX_KEY not in s.info
        await s.commit()
    await asyncio.sleep(0.05)
    assert calls == []


# ---------------------------------------------------------------- 发送失败

class _BoomClient:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("network down")


class _ErrorResponse:
    status_code = 500


class _ErrorClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return _ErrorResponse()


async def test_send_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(im_webhook, "http_client", lambda **kw: _BoomClient())
    await im_webhook._send_one(HOOK, {"x": 1})  # 不抛

    monkeypatch.setattr(im_webhook, "http_client", lambda **kw: _ErrorClient())
    await im_webhook._send_one(HOOK, {"x": 1})  # 500 也不抛


async def test_business_commit_survives_http_failure(monkeypatch):
    _use_env(monkeypatch)
    monkeypatch.setattr(im_webhook, "http_client", lambda **kw: _BoomClient())
    async with AsyncSessionLocal() as s:
        im_webhook.queue_notification(
            s, kind="approval.pending", title="待审批", body="b", link="/i")
        await s.commit()  # IM 挂了也不能影响业务提交
    await asyncio.sleep(0.05)
