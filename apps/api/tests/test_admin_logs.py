"""超级管理后台系统日志（V6.2）：事件查询/聊天台账/敏感查看三因子。

钉住的契约：
1. 事件日志：超管+凭据可查，支持公司/类型过滤；
2. 聊天线程目录不需动态码（元数据），但**内容**必须：原因（≥5字）+
   X-TOTP-Code 动态码验证，每次查看落 sensitive_access 台账；
3. 坏原因/错动态码 → 400/403；非超管 → 403；
4. 敏感访问台账完整可查。
"""
from __future__ import annotations

import pyotp
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _clean_totp_state():
    from app.services import totp_service
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


async def _seed_chat(company_id: str) -> str:
    from app.db.session import AsyncSessionLocal
    from app.models.agent import Agent
    from app.models.agent_chat import AgentChatMessage
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=company_id, name="日志测试员工")
        db.add(agent)
        await db.flush()
        aid = agent.id
        db.add(AgentChatMessage(
            id=str(uuid.uuid4()), company_id=company_id, agent_id=aid,
            thread_id="th-1", role="boss", content="客户合同里约定了保密条款"))
        db.add(AgentChatMessage(
            id=str(uuid.uuid4()), company_id=company_id, agent_id=aid,
            thread_id="th-1", role="agent", content="收到，已按条款执行"))
        await db.commit()
    return aid


async def test_events_log_query_and_filters(client, tenants, make_user):
    admin = await _make_superadmin(make_user)
    from app.db.session import AsyncSessionLocal
    from app.services.event_system import log_event
    async with AsyncSessionLocal() as db:
        await log_event(db, "systest.alpha", "company",
                        tenants.alice_co["id"], "user", admin["id"], {},
                        company_id=tenants.alice_co["id"])
        await log_event(db, "systest.beta", "company",
                        tenants.bob_co["id"], "user", admin["id"], {},
                        company_id=tenants.bob_co["id"])
        await db.commit()

    r = await client.get(
        f"/ops/admin/logs/events?company_id={tenants.alice_co['id']}",
        headers=_key(admin))
    assert r.status_code == 200, r.text
    evs = r.json()["events"]
    assert all(e["company_id"] == tenants.alice_co["id"] for e in evs)
    assert any(e["event_type"] == "systest.alpha" for e in evs)
    assert not any(e["event_type"] == "systest.beta" for e in evs)

    r = await client.get(
        f"/ops/admin/logs/events?event_type=systest.alpha",
        headers=_key(admin))
    assert all(e["event_type"] == "systest.alpha" for e in r.json()["events"])


async def test_chat_content_requires_reason_and_totp(client, tenants,
                                                     make_user):
    admin = await _make_superadmin(make_user)
    co = tenants.alice_co["id"]
    aid = await _seed_chat(co)
    h = _key(admin)

    # 线程目录（元数据）无需动态码
    r = await client.get(f"/ops/admin/logs/chats?company_id={co}", headers=h)
    assert r.status_code == 200, r.text
    threads = r.json()["threads"]
    assert threads and threads[0]["messages"] == 2

    # 内容：缺原因 → 400
    r = await client.get(
        f"/ops/admin/logs/chats/{aid}/messages?company_id={co}"
        f"&reason=ab", headers={**h, "X-TOTP-Code": "123456"})
    assert r.status_code == 400

    # 内容：原因短 → 400
    r = await client.get(
        f"/ops/admin/logs/chats/{aid}/messages?company_id={co}"
        f"&reason=审计", headers={**h, "X-TOTP-Code": "123456"})
    assert r.status_code == 400

    # 内容：未绑定 TOTP → 403（服务端无 TOTP 状态视为未绑定）
    r = await client.get(
        f"/ops/admin/logs/chats/{aid}/messages?company_id={co}"
        f"&reason=合规抽查-客户投诉核查",
        headers={**h, "X-TOTP-Code": "654321"})
    assert r.status_code == 403

    # 绑定 TOTP 后：动态码 → 200 + 台账落行
    from urllib.parse import urlparse, parse_qs
    r = await client.post("/ops/admin/2fa/enroll", headers=h,
                          json={"account": "auditor@aieos"})
    secret = parse_qs(urlparse(r.json()["otpauth_uri"]).query)["secret"][0]
    r = await client.post("/ops/admin/2fa/confirm", headers=h,
                          json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200

    code = pyotp.TOTP(secret).now()
    r = await client.get(
        f"/ops/admin/logs/chats/{aid}/messages?company_id={co}"
        f"&reason=合规抽查-客户投诉核查",
        headers={**h, "X-TOTP-Code": code})
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert len(msgs) == 2  # audited 标记在响应顶层（路由返回 audited=True）

    # 台账：敏感访问有记录（含 reason 与 auth=totp）
    r = await client.get("/ops/admin/logs/sensitive-access", headers=h)
    acc = r.json()["access"]
    assert acc and acc[0]["category"] == "chat_thread"
    assert "合规抽查" in acc[0]["reason"]

    # 错动态码 → 403
    r = await client.get(
        f"/ops/admin/logs/chats/{aid}/messages?company_id={co}"
        f"&reason=合规抽查-再来一次",
        headers={**h, "X-TOTP-Code": "999999"})
    assert r.status_code == 403


import uuid  # noqa: E402


async def test_admin_gate_blocks_non_admin(client, tenants, make_user):
    r = await client.get(
        f"/ops/admin/logs/events?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"])
    assert r.status_code == 403
    r = await client.get(f"/ops/admin/logs/chats?company_id={tenants.alice_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 403
