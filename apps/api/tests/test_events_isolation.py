"""审计事件日志的租户隔离（P0-2）。

EventLog 原先没有 company_id，`GET /events` 与 `/events/export` 会无条件
返回全平台审计日志（actor_id / task_id / payload），任何登录用户都能导出
别人的操作记录。本组用例用两个真实租户证明：

* A 写入的事件，B 读不到；B 写入的事件，A 也读不到；
* A 自己写入的事件必须能看到（防止“把接口锁死”式的假修复）；
* 导出接口与列表接口行为一致；
* 历史 company_id 为 NULL 的行不会因为“没有归属”而被误当成公共数据；
* log_event 能按实体反查公司，反查失败时降级为不可见且不留半条记录。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.event import EventLog
from app.services.event_system import log_event

EVENT_TYPE = "isolation.test"


async def _seed_event(company_id: str | None, marker: str,
                      actor_id: str | None = None) -> str:
    """直接落一条事件，模拟业务代码写入的审计记录。"""
    async with AsyncSessionLocal() as db:
        event = EventLog(
            event_type=EVENT_TYPE,
            entity_type=None,
            entity_id=None,
            actor_type="user",
            actor_id=actor_id,
            payload={"marker": marker},
            company_id=company_id,
        )
        db.add(event)
        await db.commit()
        return event.id


@pytest.fixture
async def seeded(tenants) -> SimpleNamespace:
    """双租户各一条事件，外加一条历史遗留（company_id=NULL）事件。"""
    a_event = await _seed_event(tenants.alice_co["id"], "only-alice",
                                actor_id="actor-alice")
    b_event = await _seed_event(tenants.bob_co["id"], "only-bob",
                                actor_id="actor-bob")
    legacy_event = await _seed_event(None, "legacy-no-company")
    return SimpleNamespace(a_event=a_event, b_event=b_event,
                           legacy_event=legacy_event)


def _markers(rows: list[dict]) -> set[str]:
    return {(row.get("payload") or {}).get("marker") for row in rows}


async def test_event_list_only_returns_own_tenant(client, tenants, seeded):
    r = await client.get("/events", params={"event_type": EVENT_TYPE},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, f"A 读取自己的事件失败：{r.status_code} {r.text}"
    a_rows = r.json()
    a_markers = _markers(a_rows)
    assert "only-alice" in a_markers, (
        f"A 看不到自己写入的事件，事件列表={a_rows}")
    assert "only-bob" not in a_markers, (
        f"租户隔离失效：A 看到了 B 的事件，事件列表={a_rows}")
    assert "legacy-no-company" not in a_markers, (
        f"company_id 为空的历史事件被当成了公共数据返回，事件列表={a_rows}")

    r = await client.get("/events", params={"event_type": EVENT_TYPE},
                         headers=tenants.bob["headers"])
    assert r.status_code == 200, f"B 读取自己的事件失败：{r.status_code} {r.text}"
    b_rows = r.json()
    b_markers = _markers(b_rows)
    assert "only-bob" in b_markers, (
        f"B 看不到自己写入的事件，事件列表={b_rows}")
    assert "only-alice" not in b_markers, (
        f"租户隔离失效：B 看到了 A 的事件，事件列表={b_rows}")
    assert "legacy-no-company" not in b_markers, (
        f"company_id 为空的历史事件被当成了公共数据返回，事件列表={b_rows}")


async def test_event_list_rejects_foreign_company_filter(client, tenants, seeded):
    """B 显式传 A 的 company_id 不能成为绕过隔离的入口。"""
    r = await client.get("/events", params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, (
        f"B 指定 A 的 company_id 竟能读取事件（应为 404）："
        f"{r.status_code} {r.text[:200]}")

    # 反向对照：属主显式过滤自己的公司必须仍然可用。
    r = await client.get("/events", params={"company_id": tenants.alice_co["id"],
                                            "event_type": EVENT_TYPE},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, f"属主过滤自己的公司失败：{r.text[:200]}"
    assert "only-alice" in _markers(r.json())


async def test_event_export_only_returns_own_tenant(client, tenants, seeded):
    r = await client.get("/events/export",
                         params={"format": "json", "event_type": EVENT_TYPE},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, f"A 导出失败：{r.status_code} {r.text}"
    a_export = json.loads(r.text)
    a_markers = _markers(a_export)
    assert "only-alice" in a_markers, (
        f"A 的导出里没有自己的事件：{a_export}")
    assert "only-bob" not in a_markers, (
        f"导出泄露：A 导出了 B 的事件：{a_export}")
    assert "legacy-no-company" not in a_markers, (
        f"导出泄露：company_id 为空的历史事件被导出：{a_export}")

    r = await client.get("/events/export",
                         params={"format": "csv", "event_type": EVENT_TYPE},
                         headers=tenants.bob["headers"])
    assert r.status_code == 200, f"B 导出失败：{r.status_code} {r.text}"
    assert "only-bob" in r.text, f"B 的 CSV 导出里没有自己的事件：{r.text[:300]}"
    assert "only-alice" not in r.text, (
        f"导出泄露：B 的 CSV 里出现了 A 的事件：{r.text[:300]}")
    assert "legacy-no-company" not in r.text, (
        f"导出泄露：B 的 CSV 里出现了无归属历史事件：{r.text[:300]}")


async def test_event_export_rejects_foreign_company_filter(client, tenants, seeded):
    r = await client.get("/events/export",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, (
        f"B 指定 A 的 company_id 竟能导出事件（应为 404）："
        f"{r.status_code} {r.text[:200]}")


async def test_log_event_resolves_company_id_from_agent(client, tenants):
    """调用方不传 company_id 时，按 agent 实体反查并写入归属。"""
    r = await client.post("/agents", json={
        "company_id": tenants.alice_co["id"], "name": "A 的员工",
        "role": "research", "department": "研究部"},
        headers=tenants.alice["headers"])
    assert r.status_code == 201, f"建 Agent 失败：{r.status_code} {r.text}"
    agent_id = r.json()["id"]

    async with AsyncSessionLocal() as db:
        await log_event(db, "isolation.autoresolve", "agent", agent_id,
                        "user", tenants.alice["id"], {"marker": "auto-resolved"})
        await db.commit()

    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(EventLog).where(
            EventLog.event_type == "isolation.autoresolve"))).scalar_one()
    assert row.company_id == tenants.alice_co["id"], (
        f"log_event 未能按 agent 反查 company_id：得到 {row.company_id!r}，"
        f"期望 {tenants.alice_co['id']!r}")

    r = await client.get("/events",
                         params={"event_type": "isolation.autoresolve"},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert "auto-resolved" in _markers(r.json()), (
        f"自动归属后的事件在 A 的列表里看不到：{r.json()}")


async def test_log_event_unresolvable_entity_is_written_as_invisible(
        client, tenants):
    """反查失败不能抛异常，也不能写半条记录；落库后必须不可见。"""
    async with AsyncSessionLocal() as db:
        await log_event(db, "isolation.unresolved", "agent",
                        "does-not-exist", "user", None, {"marker": "orphan"})
        await db.commit()

        rows = (await db.execute(select(EventLog).where(
            EventLog.event_type == "isolation.unresolved"))).scalars().all()
    assert len(rows) == 1, f"反查失败时事件应完整写入一条，实际 {len(rows)} 条"
    assert rows[0].company_id is None, (
        f"反查失败时不能猜测归属，实际写入了 {rows[0].company_id!r}")

    r = await client.get("/events", params={"event_type": "isolation.unresolved"},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert r.json() == [], (
        f"无公司归属的事件不应被任何租户看到，实际返回 {r.json()}")
