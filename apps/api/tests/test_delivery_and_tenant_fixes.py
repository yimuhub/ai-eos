"""第四轮修复的护栏：交付中心路径解析 + 三处跨租户越权。

每个用例都对应一个"用户真实会踩到"的故障：
- 交付中心把相对路径按 CWD 解析 → 门门文件都显示"丢失"、点了 404
- 记忆/自动化/作战室的路径里没有 company_id → router 级守卫直接跳过
"""
from __future__ import annotations

import uuid

import pytest

from app.api.routes.assets import storage_root
from app.db.session import AsyncSessionLocal
from app.models.asset import Asset, AssetType
from app.models.war_room import WarRoomSession

pytestmark = pytest.mark.asyncio


async def _add_asset(company_id: str, user_id: str, name: str,
                     content: str) -> str:
    """按线上真实写法落盘：DB 里只存相对 storage 根的路径。"""
    root = storage_root().resolve()
    fname = f"{uuid.uuid4().hex[:8]}_{name}"
    fpath = root / "artifacts" / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content, encoding="utf-8")
    async with AsyncSessionLocal() as s:
        a = Asset(company_id=company_id, name=name, type=AssetType.REPORT,
                  path=str(fpath.relative_to(root)), size=len(content.encode()),
                  created_by=user_id, creator_type="agent")
        s.add(a)
        await s.commit()
        return a.id


async def test_delivery_lists_and_downloads_relative_path(client, make_user,
                                                          make_company):
    user = await make_user()
    co = await make_company(user)
    aid = await _add_asset(co["id"], user["id"], "季度报告.md", "# 真实内容\n")

    r = await client.get(f"/delivery/by-project?company_id={co['id']}",
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    files = [f for g in r.json()["groups"] for f in g["files"]]
    target = next(f for f in files if f["id"] == aid)
    assert target["exists"] is True, "相对路径必须按 storage 根还原，否则文件永远显示丢失"

    r = await client.get(f"/delivery/download/{aid}", headers=user["headers"])
    assert r.status_code == 200, f"下载失败：{r.status_code} {r.text}"
    assert "# 真实内容" in r.text


async def test_memory_recall_rejects_foreign_agent(client, tenants):
    r = await client.post("/agents", json={
        "company_id": tenants.bob_co["id"], "name": "Bob 的分析师"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    bob_agent = r.json()["id"]

    # Alice 拿自己的 company_id + Bob 的 agent_id：修复前能读到记忆原文
    r = await client.get(
        f"/memory/recall?company_id={tenants.alice_co['id']}&agent_id={bob_agent}",
        headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户记忆读取未被拦截：{r.status_code} {r.text}"


async def test_memory_remember_rejects_foreign_agent(client, tenants):
    r = await client.post("/agents", json={
        "company_id": tenants.bob_co["id"], "name": "Bob 的助理"},
        headers=tenants.bob["headers"])
    bob_agent = r.json()["id"]

    r = await client.post(
        f"/memory/remember?company_id={tenants.alice_co['id']}",
        json={"agent_id": bob_agent, "memory_type": "SEMANTIC",
              "content": "注入内容", "importance": 0.9},
        headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户写记忆未被拦截：{r.status_code} {r.text}"


async def test_automation_run_rejects_foreign_definition(client, tenants):
    r = await client.post(f"/automations?company_id={tenants.bob_co['id']}",
                          json={"name": "Bob 的自动化", "steps": [
                              {"type": "create_task", "params": {"title": "x"}}]},
                          headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    # 路径里没有 company_id：router 级守卫会跳过，必须靠端点自己比对归属
    r = await client.post(f"/automations/{aid}/run",
                          headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户执行自动化未被拦截：{r.status_code} {r.text}"

    r = await client.patch(f"/automations/{aid}", json={"action": "archive"},
                           headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户归档自动化未被拦截：{r.status_code} {r.text}"


async def test_war_room_session_rejects_foreign_company(client, tenants):
    """company_id 属于自己、sid 属于别人：会话级归属校验必须挡住。"""
    sid = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        s.add(WarRoomSession(id=sid, company_id=tenants.bob_co["id"],
                             goal="Bob 的目标", leader_agent_id="leader-x",
                             status="executing"))
        await s.commit()

    r = await client.get(
        f"/war-room/{sid}?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"])
    assert r.status_code == 404, f"跨租户读取作战室未被拦截：{r.status_code} {r.text}"

    r = await client.get(
        f"/war-room/{sid}?company_id={tenants.bob_co['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 200, f"自己公司的会话必须能读：{r.status_code} {r.text}"
