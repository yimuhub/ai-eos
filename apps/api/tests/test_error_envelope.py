"""A3 统一错误信封：状态码不变、detail 保留、code/message/request_id 齐全。

守的是前端与排障的共同契约：

* 状态码是前端分支依据，任何改造都不得改写；
* ``detail`` 是现有前端/测试读取的字段，原样保留（字符串 / 422 errors 列表）；
* ``code`` 给外部集成方稳定分支，``message`` 给用户看，``request_id``
  把响应、日志、截图串成同一次请求；
* 响应头 X-Request-ID 与响应体里的 request_id 必须一致。
"""
from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from app.main import _unhandled_exception_handler
from app.core.request_context import REQUEST_ID_HEADER

pytestmark = pytest.mark.asyncio


def _request_with_state(rid: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/boom",
        "raw_path": b"/boom",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "state": {"request_id": rid},
    }
    return Request(scope)


def _assert_envelope(body: dict, *, detail, code: str, header_rid: str) -> None:
    """信封四要素齐全，且 request_id 与响应头一致。"""
    assert body["detail"] == detail, "detail 必须保持原值（前端/测试依赖它）"
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"], "message 不能为空"
    assert body["request_id"] == header_rid, "响应体与响应头的 request_id 必须一致"


# ---------------------------------------------------------------------------
# 401
# ---------------------------------------------------------------------------
async def test_401_keeps_status_and_detail(client):
    r = await client.get("/subscription", params={"company_id": "c-1"})

    assert r.status_code == 401, r.text
    body = r.json()
    _assert_envelope(body, detail="Not authenticated", code="unauthorized",
                     header_rid=r.headers[REQUEST_ID_HEADER])
    # 字符串 detail 直接复用为 message，前端展示的仍是后端原话
    assert body["message"] == "Not authenticated"


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------
async def test_404_unknown_route(client):
    r = await client.get("/definitely-not-a-real-route-xyz")

    assert r.status_code == 404, r.text
    body = r.json()
    _assert_envelope(body, detail="Not Found", code="not_found",
                     header_rid=r.headers[REQUEST_ID_HEADER])


async def test_404_domain_detail_is_preserved(client, tenants):
    """业务 404 的 detail 是给用户看的中文，不能被通用文案覆盖。"""
    r = await client.get(
        "/subscription", params={"company_id": "does-not-exist"},
        headers=tenants.alice["headers"])

    assert r.status_code == 404, r.text
    body = r.json()
    assert body["detail"] == "Company not found"
    assert body["code"] == "not_found"
    assert body["message"] == "Company not found"


async def test_error_response_keeps_cors_headers(client):
    """错误响应也必须带 CORS 头，否则浏览器端只看到网络错误而非可读的 404。"""
    r = await client.get("/definitely-not-a-real-route-xyz",
                         headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 404
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# 409
# ---------------------------------------------------------------------------
async def test_409_repeat_trial(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    first = await client.post("/subscription/trial",
                              json={"company_id": co["id"]},
                              headers=alice["headers"])
    assert first.status_code == 200, first.text

    r = await client.post("/subscription/trial",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 409, r.text
    body = r.json()
    _assert_envelope(body, detail="该企业已领取过试用，不能重复领取",
                     code="conflict",
                     header_rid=r.headers[REQUEST_ID_HEADER])
    assert body["message"] == body["detail"]


# ---------------------------------------------------------------------------
# 422
# ---------------------------------------------------------------------------
async def test_422_keeps_raw_errors_list(client, tenants):
    """422 的 detail 必须仍是 FastAPI 原始 errors 列表（前端按 loc 定位字段）。"""
    r = await client.post("/subscription/cancel", json={},
                          headers=tenants.alice["headers"])

    assert r.status_code == 422, r.text
    body = r.json()
    assert isinstance(body["detail"], list) and body["detail"], "detail 不能退化成字符串"
    assert any("company_id" in [str(p) for p in err.get("loc", [])]
               for err in body["detail"])
    _assert_envelope(body, detail=body["detail"], code="validation_error",
                     header_rid=r.headers[REQUEST_ID_HEADER])
    # 复杂 detail 用通用文案，而不是把 list 强转成字符串
    assert body["message"] == "请求参数校验失败"


# ---------------------------------------------------------------------------
# 500
# ---------------------------------------------------------------------------
async def test_500_keeps_existing_fields_and_adds_code():
    rid = "err-12345678"
    response = await _unhandled_exception_handler(
        _request_with_state(rid), RuntimeError("boom"))

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == rid
    body = json.loads(response.body)
    # 原有字段必须保留：前端与日志排障都依赖它们
    assert body["error_type"] == "RuntimeError"
    assert body["detail"]
    assert body["request_id"] == rid
    assert body["code"] == "internal_error"
    assert body["message"] == body["detail"]
