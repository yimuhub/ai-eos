"""安全面（V6 M5）：出站 DLP、审计导出、自然人↔Agent 绑定。

钉住的契约：
1. DLP：手机号/身份证/银行卡在工具结果里默认打码（fail-safe）；
   dlp:outbound 开关关闭后放行原文；命中进审计；
2. 绑定：1:N 归属、relation 枚举、员工归属校验、租户隔离；
3. 审计导出：MANAGE 闸门，CSV 头与行数。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.services.dlp import mask_result, mask_text, scan_text


# ---------------------------------------------------------------- DLP 纯函数

def test_dlp_masks_phone_id_bank():
    text = "客户张三 13812345678，身份证 110101199003077512，卡号 6222020200112233445"
    masked, findings = mask_text(text)
    assert "13812345678" not in masked
    assert "110101199003077512" not in masked
    assert "6222020200112233445" not in masked
    assert "138****5678" in masked
    kinds = {f["type"] for f in findings}
    assert {"phone", "id_card", "bank"} <= kinds


def test_dlp_scan_counts():
    findings = scan_text("电话 13900000000 和 13811112222")
    phone = next(f for f in findings if f["type"] == "phone")
    assert phone["count"] == 2


def test_dlp_mask_result_recursive():
    result = {"results": [{"title": "t", "body": "联系 15812345678"}],
             "note": "无敏感"}
    masked, findings = mask_result(result)
    assert "15812345678" not in json_safe(masked)
    assert any(f["type"] == "phone" for f in findings)


def json_safe(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


async def test_dlp_in_bus_and_flag_off(tenants, monkeypatch):
    co = tenants.alice_co["id"]

    async def fake_skill(slug, payload):
        return {"result": {"results": [{"body": "客户电话 13712345678"}]}}

    import app.skill_system as skill_mod
    monkeypatch.setattr(skill_mod, "execute_skill", fake_skill)
    res = await execute_bus_tool("web_search", {"query": "客户"},
                                 company_id=co, agent_id="a1")
    assert "13712345678" not in json_safe(res)
    assert "137****5678" in json_safe(res)

    # 关闭开关 → 原文放行
    from app.models.feature_flags import CompanyFeatureFlag
    async with AsyncSessionLocal() as db:
        db.add(CompanyFeatureFlag(
            id=str(uuid.uuid4()), company_id=co, key="dlp:outbound",
            value={}, enabled=False, updated_by=None))
        await db.commit()
    res = await execute_bus_tool("web_search", {"query": "客户"},
                                 company_id=co, agent_id="a1")
    assert "13712345678" in json_safe(res)


from app.toolbus import execute_bus_tool  # noqa: E402


# ---------------------------------------------------------------- 绑定

async def _make_agent(company_id: str, name: str) -> str:
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=company_id, name=name)
        db.add(agent)
        await db.commit()
        return agent.id


async def test_member_agent_bindings_flow(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    a1 = await _make_agent(co, "随身助理")
    a2 = await _make_agent(co, "销售下属")

    r = await client.put(
        f"/member-agents?company_id={co}", headers=alice["headers"],
        json={"user_id": alice["id"], "bindings": [
            {"agent_id": a1, "relation": "assistant"},
            {"agent_id": a2, "relation": "subordinate"}]})
    assert r.status_code == 200 and r.json()["count"] == 2

    r = await client.get(
        f"/member-agents?company_id={co}&user_id={alice['id']}",
        headers=alice["headers"])
    items = r.json()["bindings"]
    assert len(items) == 2
    assert {i["relation"] for i in items} == {"assistant", "subordinate"}
    assert {i["agent_name"] for i in items} == {"随身助理", "销售下属"}

    # 坏 relation / 别公司的员工 → 400
    r = await client.put(
        f"/member-agents?company_id={co}", headers=alice["headers"],
        json={"user_id": alice["id"], "bindings": [
            {"agent_id": a1, "relation": "boss_of_world"}]})
    assert r.status_code == 400
    bob_agent = await _make_agent(tenants.bob_co["id"], "别人家的员工")
    r = await client.put(
        f"/member-agents?company_id={co}", headers=alice["headers"],
        json={"user_id": alice["id"], "bindings": [
            {"agent_id": bob_agent, "relation": "assistant"}]})
    assert r.status_code == 400

    # 整表重置：只留一个
    r = await client.put(
        f"/member-agents?company_id={co}", headers=alice["headers"],
        json={"user_id": alice["id"], "bindings": [
            {"agent_id": a1, "relation": "assistant"}]})
    r = await client.get(
        f"/member-agents?company_id={co}&user_id={alice['id']}",
        headers=alice["headers"])
    assert len(r.json()["bindings"]) == 1

    # bob 完全看不到 alice 的绑定
    r = await client.get(
        f"/member-agents?company_id={co}&user_id={alice['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_audit_export_csv(client, tenants):
    alice, co = tenants.alice, tenants.alice_co["id"]
    # 制造一条事件
    from app.services.event_system import log_event
    async with AsyncSessionLocal() as db:
        await log_event(db, "security.test", "company", co, "user",
                        alice["id"], {"hello": "world"}, company_id=co)
        await db.commit()
    r = await client.get(f"/audit/export?company_id={co}",
                         headers=alice["headers"])
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    text = r.text
    assert text.splitlines()[0].startswith("created_at,event_type")
    assert "security.test" in text
    # bob 不能导 alice 的审计
    r = await client.get(f"/audit/export?company_id={co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404
