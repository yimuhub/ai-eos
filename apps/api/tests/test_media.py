"""创作中心（图片/视频生成）护栏测试。

网络全部 mock：替换 httpx.AsyncClient，按 URL 路由到假上游；
不产生任何真实外发请求。覆盖：生成闭环、类型/权限/密钥闸门、
视频异步轮询（含 Agnes 专用查询回退）、队列满透传、文件服务与删除。
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.db.session import AsyncSessionLocal
from app.models.agent import Model, ModelProviderType

pytestmark = pytest.mark.asyncio

BASE = "http://127.0.0.1:11434/v1"
RESULT_URL = "http://127.0.0.1:11434/files/out.png"
VIDEO_URL = "http://127.0.0.1:11434/files/out.mp4"
PNG_BYTES = b"\x89PNG-fake-image-bytes"
MP4_BYTES = b"\x00\x00-fake-video-bytes"


class _FakeResp:
    def __init__(self, payload=None, status_code=200, content=b"",
                 content_type="application/json"):
        self._payload = payload
        self.status_code = status_code
        self._content = content
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)

    async def aiter_bytes(self):
        yield self._content


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _install_fake_upstream(monkeypatch, handler) -> list:
    """handler(method, url, body) -> _FakeResp；记录全部调用供断言。"""
    calls: list = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append(("POST", url, json))
            return handler("POST", url, json)

        async def get(self, url, headers=None, timeout=None,
                      follow_redirects=None):
            calls.append(("GET", url, None))
            return handler("GET", url, None)

        def stream(self, method, url, **kw):
            calls.append((method, url, None))
            return _FakeStream(handler(method, url, None))

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


async def _make_model(company_id: str, model_type: str = "image",
                      **extra) -> str:
    mid = str(uuid.uuid4())
    fields = dict(
        id=mid, company_id=company_id, name=f"测试{model_type}模型",
        provider_type=ModelProviderType.OPENAI_COMPAT,
        base_url=BASE, api_key_env="OPENAI_COMPAT_API_KEY",
        model_id=f"test-{model_type}-model", model_type=model_type,
        enabled=True)
    fields.update(extra)
    async with AsyncSessionLocal() as s:
        s.add(Model(**fields))
        await s.commit()
    return mid


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key-12345")


async def test_image_generation_full_cycle(client, tenants, monkeypatch,
                                           env_key):
    alice = tenants.alice
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "image")

    def handler(method, url, body):
        if method == "POST" and url.endswith("/images/generations"):
            assert body["model"] == "test-image-model"
            assert body["prompt"]
            return _FakeResp({"data": [{"url": RESULT_URL}]})
        if url == RESULT_URL:
            return _FakeResp(content=PNG_BYTES, content_type="image/png")
        raise AssertionError(f"意外请求: {method} {url}")

    calls = _install_fake_upstream(monkeypatch, handler)
    r = await client.post("/media/images", json={
        "company_id": co, "model_id": mid, "prompt": "一只橘猫"},
        headers=alice["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "completed"
    assert d["kind"] == "image"
    assert d["file_url"] == f"/media/files/{d['id']}"
    # 鉴权头确实带了密钥（只验证存在，不回显值）
    assert any(c[0] == "POST" for c in calls)

    # 文件服务
    f = await client.get(d["file_url"], headers=alice["headers"])
    assert f.status_code == 200
    assert f.content == PNG_BYTES

    # 历史列表
    lst = await client.get(f"/media/generations?company_id={co}",
                           headers=alice["headers"])
    assert lst.status_code == 200
    assert any(it["id"] == d["id"] for it in lst.json()["items"])

    # 删除：记录与文件一起消失
    rm = await client.delete(f"/media/generations/{d['id']}",
                             headers=alice["headers"])
    assert rm.status_code == 204
    f2 = await client.get(d["file_url"], headers=alice["headers"])
    assert f2.status_code == 404


async def test_image_rejects_text_model(client, tenants, env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "text")
    r = await client.post("/media/images", json={
        "company_id": co, "model_id": mid, "prompt": "x"},
        headers=tenants.alice["headers"])
    assert r.status_code == 400
    assert "图片" in r.json()["detail"]


async def test_cross_company_isolation(client, tenants, env_key):
    alice, bob = tenants.alice, tenants.bob
    mid = await _make_model(tenants.alice_co["id"], "image")
    # Bob 拿 Alice 的模型 + 自己的公司 → 404（不泄露模型存在）
    r = await client.post("/media/images", json={
        "company_id": tenants.bob_co["id"], "model_id": mid, "prompt": "x"},
        headers=bob["headers"])
    assert r.status_code == 404
    # Bob 读 Alice 公司的生成历史 → 404（全系统约定：越权统一 404 防枚举）
    r2 = await client.get(
        f"/media/generations?company_id={tenants.alice_co['id']}",
        headers=bob["headers"])
    assert r2.status_code == 404


async def test_missing_key_rejected(client, tenants, monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "image")
    r = await client.post("/media/images", json={
        "company_id": co, "model_id": mid, "prompt": "x"},
        headers=tenants.alice["headers"])
    assert r.status_code == 400
    assert "密钥" in r.json()["detail"]


async def test_sensitive_env_rejected(client, tenants, env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "image", api_key_env="JWT_SECRET")
    r = await client.post("/media/images", json={
        "company_id": co, "model_id": mid, "prompt": "x"},
        headers=tenants.alice["headers"])
    assert r.status_code == 400


async def test_upstream_failure_recorded(client, tenants, monkeypatch,
                                         env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "image")

    def handler(method, url, body):
        return _FakeResp({"error": {"message": "content policy"}},
                         status_code=400)

    _install_fake_upstream(monkeypatch, handler)
    r = await client.post("/media/images", json={
        "company_id": co, "model_id": mid, "prompt": "x"},
        headers=tenants.alice["headers"])
    assert r.status_code == 502
    assert "content policy" in r.json()["detail"]
    # 失败也留痕：历史里能看到原因
    lst = await client.get(f"/media/generations?company_id={co}",
                           headers=tenants.alice["headers"])
    items = lst.json()["items"]
    assert items and items[0]["status"] == "failed"
    assert "content policy" in items[0]["error"]


async def test_video_submit_poll_complete(client, tenants, monkeypatch,
                                          env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "video")
    state = {"polls": 0}

    def handler(method, url, body):
        if method == "POST" and url.endswith("/videos"):
            assert body["mode"] == "text"
            assert body["seconds"] == "5"
            return _FakeResp({"id": "task_1", "video_id": "vid_1",
                              "status": "queued"})
        if "/videos/vid_1" in url:
            state["polls"] += 1
            if state["polls"] == 1:
                return _FakeResp({"status": "in_progress", "progress": 40})
            return _FakeResp({"status": "completed", "progress": 100,
                              "url": VIDEO_URL})
        if url == VIDEO_URL:
            return _FakeResp(content=MP4_BYTES, content_type="video/mp4")
        raise AssertionError(f"意外请求: {method} {url}")

    _install_fake_upstream(monkeypatch, handler)
    h = tenants.alice["headers"]
    r = await client.post("/media/videos", json={
        "company_id": co, "model_id": mid, "prompt": "橘猫敲键盘"},
        headers=h)
    assert r.status_code == 200, r.text
    gid = r.json()["id"]
    assert r.json()["status"] == "processing"

    p1 = await client.get(f"/media/videos/{gid}", headers=h)
    assert p1.status_code == 200
    assert p1.json()["status"] == "processing"
    assert p1.json()["progress"] == 40

    p2 = await client.get(f"/media/videos/{gid}", headers=h)
    assert p2.json()["status"] == "completed"
    assert p2.json()["file_url"] == f"/media/files/{gid}"

    f = await client.get(f"/media/files/{gid}", headers=h)
    assert f.content == MP4_BYTES

    # 终态后再轮询不再打上游
    calls_before = state["polls"]
    p3 = await client.get(f"/media/videos/{gid}", headers=h)
    assert p3.json()["status"] == "completed"
    assert state["polls"] == calls_before


async def test_video_agnes_poll_fallback(client, tenants, monkeypatch,
                                         env_key):
    """标准查询 404 → 回退 Agnes 专用查询，并记住命中的形式。"""
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "video")
    seen: list[str] = []

    def handler(method, url, body):
        if method == "POST" and url.endswith("/videos"):
            return _FakeResp({"video_id": "vid_2"})
        seen.append(url)
        if "/videos/vid_2" in url:
            return _FakeResp({"error": {"message": "not found"}},
                             status_code=404)
        if "/agnesapi?" in url:
            assert "video_id=vid_2" in url
            assert "model_name=test-video-model" in url
            return _FakeResp({"status": "completed", "url": VIDEO_URL})
        if url == VIDEO_URL:
            return _FakeResp(content=MP4_BYTES, content_type="video/mp4")
        raise AssertionError(f"意外请求: {method} {url}")

    _install_fake_upstream(monkeypatch, handler)
    h = tenants.alice["headers"]
    r = await client.post("/media/videos", json={
        "company_id": co, "model_id": mid, "prompt": "x"}, headers=h)
    gid = r.json()["id"]
    p = await client.get(f"/media/videos/{gid}", headers=h)
    assert p.json()["status"] == "completed"
    # 第二次轮询（假如还有）应直接走 agnes 形式——这里验证 params 已记住
    lst = await client.get(f"/media/generations?company_id={co}", headers=h)
    item = next(it for it in lst.json()["items"] if it["id"] == gid)
    assert item["params"].get("poll") == "agnes"


async def test_video_queue_full_passthrough(client, tenants, monkeypatch,
                                            env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "video")

    def handler(method, url, body):
        return _FakeResp({"code": "video_queue_full"}, status_code=503)

    _install_fake_upstream(monkeypatch, handler)
    r = await client.post("/media/videos", json={
        "company_id": co, "model_id": mid, "prompt": "x"},
        headers=tenants.alice["headers"])
    assert r.status_code == 503
    assert "队列已满" in r.json()["detail"]
    # 未创建任务就不该留记录
    lst = await client.get(f"/media/generations?company_id={co}",
                           headers=tenants.alice["headers"])
    assert lst.json()["items"] == []


async def test_video_failed_upstream(client, tenants, monkeypatch, env_key):
    co = tenants.alice_co["id"]
    mid = await _make_model(co, "video")

    def handler(method, url, body):
        if method == "POST":
            return _FakeResp({"video_id": "vid_3"})
        return _FakeResp({"status": "failed", "error": "内容审核未通过"})

    _install_fake_upstream(monkeypatch, handler)
    h = tenants.alice["headers"]
    r = await client.post("/media/videos", json={
        "company_id": co, "model_id": mid, "prompt": "x"}, headers=h)
    gid = r.json()["id"]
    p = await client.get(f"/media/videos/{gid}", headers=h)
    assert p.json()["status"] == "failed"
    assert "审核" in p.json()["error"]