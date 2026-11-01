"""商业化底座：离线 License / 席位 / 用量 / 账单 CSV。

## 密钥从哪来

Ed25519 密钥对在**测试进程内**生成（模块级夹具），私钥只存在于内存：
不落仓库、不写临时文件，测试结束随进程消失。签名/验签走与生产
完全相同的 `license_service.canonical_payload`。

## 不碰真实库 / 不碰真实 License

所有 DB 操作走 conftest 的临时 SQLite（`AsyncSessionLocal`）；
License 通过 `monkeypatch` 注入 `AIEOS_LICENSE` / `AIEOS_LICENSE_FILE` /
`AIEOS_LICENSE_PUBKEY`，文件只写到 pytest 的 `tmp_path`。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
from datetime import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.asset import Asset
from app.models.intervention import ApprovalRequest
from app.models.membership import CompanyMember
from app.models.token_usage import TokenUsage
from app.services import license_service

FUTURE = "2099-12-31T23:59:59Z"
PAST = "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def signing_key() -> Ed25519PrivateKey:
    """本进程专用的签发密钥（私钥绝不离开内存）。"""
    return Ed25519PrivateKey.generate()


@pytest.fixture
def other_key() -> Ed25519PrivateKey:
    """另一把密钥，用于"公钥不匹配"用例。"""
    return Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def _isolate_license(monkeypatch, tmp_path):
    """每个用例都从"未配置 License"开始，且默认文件指向 tmp。"""
    monkeypatch.delenv(license_service.LICENSE_ENV, raising=False)
    monkeypatch.delenv(license_service.PUBKEY_ENV, raising=False)
    monkeypatch.delenv("AIEOS_BILLING_CURRENCY", raising=False)
    monkeypatch.setenv(license_service.LICENSE_FILE_ENV,
                       str(tmp_path / "license.json"))
    license_service.reset_cache()
    yield
    license_service.reset_cache()


def _pub_b64(key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _payload(**overrides) -> dict:
    payload = {
        "licensee": "测试公司",
        "instance": "test-instance-1",
        "seats": 5,
        "features": ["usage", "billing"],
        "expires_at": FUTURE,
        "issued_at": "2026-01-01T00:00:00Z",
        "version": 1,
    }
    payload.update(overrides)
    return payload


def _sign(payload: dict, key: Ed25519PrivateKey) -> str:
    signature = key.sign(license_service.canonical_payload(payload))
    return json.dumps({"payload": payload,
                       "signature": base64.b64encode(signature).decode()})


def _install_inline(monkeypatch, raw: str, key: Ed25519PrivateKey) -> None:
    monkeypatch.setenv(license_service.LICENSE_ENV, raw)
    monkeypatch.setenv(license_service.PUBKEY_ENV, _pub_b64(key))
    license_service.reset_cache()


@pytest.fixture
def install_license(monkeypatch, signing_key):
    """把一份合法 License 装进环境（默认两把密钥相同）。"""

    def _install(payload: dict | None = None, *, key=None,
                 encode_base64: bool = False) -> str:
        raw = _sign(payload or _payload(), key or signing_key)
        if encode_base64:
            raw = base64.b64encode(raw.encode("utf-8")).decode()
        _install_inline(monkeypatch, raw, signing_key)
        return raw

    return _install


async def _create_agent(client, user: dict, company_id: str, name: str):
    return await client.post("/agents", json={"company_id": company_id,
                                              "name": name},
                             headers=user["headers"])


async def _count_agents(company_id: str) -> int:
    async with AsyncSessionLocal() as s:
        return int((await s.execute(
            select(func.count()).select_from(Agent).where(
                Agent.company_id == company_id))).scalar() or 0)


async def _disable_agent(agent_id: str) -> None:
    async with AsyncSessionLocal() as s:
        row = await s.get(Agent, agent_id)
        row.status = AgentStatus.DISABLED
        await s.commit()


async def _add_usage(company_id: str, model: str, day: str,
                     input_tokens: int, output_tokens: int,
                     cost: float) -> None:
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(
            company_id=company_id, model_name=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cost=cost, output_cost=0.0, total_cost=cost,
            created_at=datetime.fromisoformat(f"{day}T12:00:00"),
        ))
        await s.commit()


async def _add_asset(company_id: str, day: str, name: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(Asset(company_id=company_id, name=name,
                    path=f"artifacts/{name}",
                    created_at=datetime.fromisoformat(f"{day}T12:00:00")))
        await s.commit()


async def _add_member(company_id: str, user_id: str, role: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(CompanyMember(company_id=company_id, user_id=user_id,
                            role=role, status="active"))
        await s.commit()


# ---------------------------------------------------------------------------
# 1) License 校验边界
# ---------------------------------------------------------------------------

async def test_missing_license_is_community_and_unlimited():
    state = license_service.get_state()
    assert state.status == "missing"
    assert state.mode == "community"
    assert state.seats is None
    assert state.features == ()
    assert state.read_only is False
    assert state.verification == "demo-key"
    assert license_service.seat_limit() is None
    assert license_service.has_feature("billing") is False
    assert license_service.is_read_only() is False


async def test_valid_license_parses_unknown_features_are_ignored(install_license):
    install_license(_payload(features=["usage", "future_thing"]))
    state = license_service.get_state()
    assert state.status == "valid"
    assert state.mode == "licensed"
    assert state.seats == 5
    assert state.features == ("usage",)
    assert state.ignored_features == ("future_thing",)
    assert state.licensee == "测试公司"
    assert state.verification == "configured-key"
    assert state.read_only is False


async def test_inline_license_accepts_base64_wrapper(install_license):
    install_license(_payload(seats=2), encode_base64=True)
    state = license_service.get_state()
    assert state.status == "valid"
    assert state.seats == 2


async def test_expired_license_is_read_only(install_license):
    install_license(_payload(expires_at=PAST))
    state = license_service.get_state()
    assert state.status == "expired"
    assert state.mode == "licensed"  # 仍是已授权，只是只读
    assert state.read_only is True
    assert license_service.is_read_only() is True


async def test_tampered_payload_fails_closed(install_license, monkeypatch,
                                             signing_key):
    raw = _sign(_payload(seats=5), signing_key)
    envelope = json.loads(raw)
    envelope["payload"]["seats"] = 500  # 改内容但保留原签名
    _install_inline(monkeypatch,
                    json.dumps(envelope, ensure_ascii=False), signing_key)
    state = license_service.get_state()
    assert state.status == "invalid_signature"
    assert state.read_only is True
    assert state.seats is None


async def test_wrong_public_key_is_invalid(install_license, other_key):
    install_license(_payload(), key=other_key)
    state = license_service.get_state()
    assert state.status == "invalid_signature"
    assert state.reason == "bad_signature"


async def test_malformed_license_is_invalid(monkeypatch, signing_key):
    _install_inline(monkeypatch, "not-a-license", signing_key)
    state = license_service.get_state()
    assert state.status == "invalid_signature"
    assert state.read_only is True


async def test_file_source_and_mtime_cache_invalidation(
        monkeypatch, tmp_path, signing_key):
    path = tmp_path / "license.json"
    path.write_text(_sign(_payload(seats=4), signing_key), encoding="utf-8")
    monkeypatch.setenv(license_service.LICENSE_FILE_ENV, str(path))
    monkeypatch.setenv(license_service.PUBKEY_ENV, _pub_b64(signing_key))
    license_service.reset_cache()

    first = license_service.get_state()
    second = license_service.get_state()
    assert first.status == "valid" and first.seats == 4
    assert first.source.startswith("file:")
    assert second is first, "同一份文件应命中缓存，不重复验签"

    path.write_text(_sign(_payload(expires_at=PAST), signing_key),
                    encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    third = license_service.get_state()
    assert third.status == "expired"
    assert third is not first


# ---------------------------------------------------------------------------
# 2) /license/status
# ---------------------------------------------------------------------------

async def test_status_community_mode_for_logged_in_user(client, make_user):
    user = await make_user()
    r = await client.get("/license/status", headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "community"
    assert body["status"] == "missing"
    assert body["seats"] == {"limit": None, "used": None}
    assert body["features"] == []
    assert body["read_only"] is False
    assert body["verification"] == "demo-key"
    assert "signature" not in r.text


async def test_status_licensed_reports_company_seats_and_hides_raw(
        client, make_user, make_company, install_license):
    raw = install_license(_payload(seats=3))
    owner = await make_user()
    company = await make_company(owner)
    created = await _create_agent(client, owner, company["id"], "员工一")
    assert created.status_code == 201, created.text

    r = await client.get("/license/status",
                         params={"company_id": company["id"]},
                         headers=owner["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "licensed"
    assert body["seats"] == {"limit": 3, "used": 1}
    assert body["expires_at"].startswith("2099-12-31")
    assert raw not in r.text
    assert "signature" not in r.text


async def test_status_cross_company_is_404(client, tenants, install_license):
    install_license(_payload())
    r = await client.get("/license/status",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3) 席位闸门
# ---------------------------------------------------------------------------

async def test_seat_gate_allows_exactly_full_then_409_with_approval(
        client, make_user, make_company, install_license):
    install_license(_payload(seats=2))
    owner = await make_user()
    company = await make_company(owner)

    for name in ("员工一", "员工二"):
        r = await _create_agent(client, owner, company["id"], name)
        assert r.status_code == 201, r.text

    blocked = await _create_agent(client, owner, company["id"], "员工三")
    assert blocked.status_code == 409, blocked.text
    assert "License 席位不足" in blocked.json()["detail"]

    async with AsyncSessionLocal() as s:
        reqs = (await s.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.company_id == company["id"],
                ApprovalRequest.action_type == "license_seats"))).scalars().all()
    assert len(reqs) == 1
    assert reqs[0].payload["limit"] == 2
    assert reqs[0].payload["used"] == 2
    assert reqs[0].payload["adding"] == 1
    assert await _count_agents(company["id"]) == 2


async def test_expired_license_blocks_agent_creation_403(
        client, make_user, make_company, install_license):
    # E1 全局门禁连 POST /companies 一起挡，所以先建好租户再装过期 License
    owner = await make_user()
    company = await make_company(owner)
    install_license(_payload(expires_at=PAST))

    r = await _create_agent(client, owner, company["id"], "员工一")
    assert r.status_code == 403
    # E1 起由全局只读中间件统一拦截，detail/code 以门禁为准
    body = r.json()
    assert body["detail"] == "License 已过期或无效，系统处于只读模式；请更新 License 后重试"
    assert body["code"] == "license_read_only"
    assert await _count_agents(company["id"]) == 0


async def test_community_mode_does_not_limit_seats(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    for i in range(3):
        r = await _create_agent(client, owner, company["id"], f"员工{i}")
        assert r.status_code == 201, r.text
    r = await client.get("/license/status",
                         params={"company_id": company["id"]},
                         headers=owner["headers"])
    assert r.json()["seats"] == {"limit": None, "used": 3}


# ---------------------------------------------------------------------------
# 4) 用量聚合与 CSV
# ---------------------------------------------------------------------------

async def _seed_usage(company_id: str) -> None:
    await _add_usage(company_id, "model-a", "2026-03-02", 100, 50, 0.1)
    await _add_usage(company_id, "model-b", "2026-03-03", 200, 100, 0.2)
    # 期间外：不得计入
    await _add_usage(company_id, "model-a", "2026-02-28", 999, 999, 9.9)
    await _add_asset(company_id, "2026-03-02", "报告A.md")
    await _add_asset(company_id, "2026-03-05", "报告B.md")
    await _add_asset(company_id, "2026-02-01", "旧报告.md")


async def test_usage_aggregation_by_model_and_day(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    a1 = await _create_agent(client, owner, company["id"], "员工一")
    a2 = await _create_agent(client, owner, company["id"], "员工二")
    assert a1.status_code == 201 and a2.status_code == 201
    await _disable_agent(a2.json()["id"])
    await _seed_usage(company["id"])

    r = await client.get("/billing/usage", params={
        "company_id": company["id"], "from": "2026-03-01", "to": "2026-03-31",
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["company_id"] == company["id"]
    assert body["period"] == {"from": "2026-03-01", "to": "2026-03-31"}
    # 席位 = 在编 Agent（DISABLED 不计），社区模式不限上限
    assert body["seats"] == {"limit": None, "used": 1}
    assert body["agents_active"] == 1
    assert body["tokens"] == {"input": 300, "output": 150, "total": 450}
    assert body["cost"] == "0.30"          # 十进制字符串，不出现浮点尾巴
    assert body["currency"] == "CNY"
    assert body["deliverables"] == 2        # 只数期间内创建的 Asset
    assert [m["model"] for m in body["by_model"]] == ["model-a", "model-b"]
    assert [d["date"] for d in body["by_day"]] == ["2026-03-02", "2026-03-03"]
    assert body["by_day"][0]["cost"] == "0.10"


async def test_usage_rejects_bad_period(client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)
    r = await client.get("/billing/usage", params={
        "company_id": company["id"], "from": "2026-03-31", "to": "2026-03-01",
    }, headers=owner["headers"])
    assert r.status_code == 400


async def test_usage_cross_company_is_404(client, tenants):
    r = await client.get("/billing/usage", params={
        "company_id": tenants.alice_co["id"],
        "from": "2026-03-01", "to": "2026-03-31",
    }, headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_invoice_csv_header_row_and_decimal_money(
        client, make_user, make_company, install_license):
    install_license(_payload(seats=3))
    owner = await make_user()
    company = await make_company(owner)
    assert (await _create_agent(client, owner, company["id"], "员工一")).status_code == 201
    assert (await _create_agent(client, owner, company["id"], "员工二")).status_code == 201
    await _seed_usage(company["id"])

    r = await client.get("/billing/invoices/export.csv", params={
        "company_id": company["id"], "from": "2026-03-01", "to": "2026-03-31",
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert ".csv" in r.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == [
        "company_id", "period_start", "period_end",
        "seats_limit", "seats_used", "agents_active",
        "tokens_input", "tokens_output", "tokens_total",
        "cost", "currency", "deliverables",
    ]
    assert len(rows) == 2
    assert rows[1] == [
        company["id"], "2026-03-01", "2026-03-31",
        "3", "2", "2", "300", "150", "450", "0.30", "CNY", "2",
    ]


async def test_invoice_csv_requires_manage_and_same_company(
        client, make_user, make_company, tenants):
    owner = await make_user()
    company = await make_company(owner)
    viewer = await make_user()
    await _add_member(company["id"], viewer["id"], "viewer")

    # VIEW 足够看用量
    read = await client.get("/billing/usage", params={
        "company_id": company["id"], "from": "2026-03-01", "to": "2026-03-31",
    }, headers=viewer["headers"])
    assert read.status_code == 200, read.text

    # 账单导出是 MANAGE：viewer 与外部租户都拿不到（统一 404）
    denied = await client.get("/billing/invoices/export.csv", params={
        "company_id": company["id"],
    }, headers=viewer["headers"])
    assert denied.status_code == 404

    cross = await client.get("/billing/invoices/export.csv", params={
        "company_id": tenants.alice_co["id"],
    }, headers=tenants.bob["headers"])
    assert cross.status_code == 404
