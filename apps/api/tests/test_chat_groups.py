"""自由群聊（/chat-groups）：内置分组自动补齐、自建群增删、群聊成员归属。"""
import uuid

import pytest


async def _agents(client, company_id, user, n=3):
    out = []
    for i in range(n):
        r = await client.post("/agents", json={
            "company_id": company_id, "name": f"员工{uuid.uuid4().hex[:4]}-{i}"},
            headers=user["headers"])
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


@pytest.mark.asyncio
async def test_builtin_groups_are_listed_virtual(client, tenants):
    t = tenants
    r = await client.get(f"/chat-groups?company_id={t.alice_co['id']}",
                         headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    presets = [g for g in groups if g["kind"] == "preset"]
    assert {g["preset_id"] for g in presets} == {"marketing", "product-rd"}
    for g in presets:
        assert g["id"].startswith("preset:")
        assert g["members"] == []
        assert len(g["missing"]) >= 5
        assert g["hint"]


@pytest.mark.asyncio
async def test_ensure_preset_creates_and_reuses_agents(client, tenants):
    t = tenants
    url = "/chat-groups/preset/marketing/ensure"
    body = {"company_id": t.alice_co["id"]}
    r = await client.post(url, json=body, headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["created"]) == 5
    assert data["group"]["kind"] == "preset"
    assert len(data["group"]["members"]) == 5
    gid = data["group"]["id"]

    # 幂等：再点一次不再重复建人
    r2 = await client.post(url, json=body, headers=t.alice["headers"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["created"] == []
    assert r2.json()["group"]["id"] == gid

    # 列表里从虚拟条目变成真条目
    r3 = await client.get(f"/chat-groups?company_id={t.alice_co['id']}",
                          headers=t.alice["headers"])
    marketing = next(g for g in r3.json()["groups"] if g["preset_id"] == "marketing")
    assert marketing["id"] == gid
    assert marketing["missing"] == []


@pytest.mark.asyncio
async def test_custom_group_crud_and_tenant_isolation(client, tenants):
    t = tenants
    mine = await _agents(client, t.alice_co["id"], t.alice, 2)
    r = await client.post("/chat-groups", json={
        "company_id": t.alice_co["id"], "name": "临时攻坚群",
        "agent_ids": [a["id"] for a in mine]}, headers=t.alice["headers"])
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["kind"] == "custom" and len(g["members"]) == 2

    r = await client.patch(f"/chat-groups/{g['id']}",
                           json={"name": "攻坚群（改名）", "agent_ids": [mine[0]["id"]]},
                           headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "攻坚群（改名）"
    assert len(r.json()["members"]) == 1

    # 别家的员工不能拉进来
    other = await _agents(client, t.bob_co["id"], t.bob, 1)
    r = await client.patch(f"/chat-groups/{g['id']}",
                           json={"agent_ids": [other[0]["id"]]},
                           headers=t.alice["headers"])
    assert r.status_code == 400

    # 别的公司看不到、改不了
    r = await client.get(f"/chat-groups?company_id={t.bob_co['id']}",
                         headers=t.bob["headers"])
    assert all(x["id"] != g["id"] for x in r.json()["groups"])
    r = await client.patch(f"/chat-groups/{g['id']}", json={"name": "越权"},
                           headers=t.bob["headers"])
    assert r.status_code == 404

    r = await client.delete(f"/chat-groups/{g['id']}", headers=t.alice["headers"])
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_group_chat_uses_group_members(client, tenants):
    t = tenants
    members = await _agents(client, t.alice_co["id"], t.alice, 2)
    r = await client.post("/chat-groups", json={
        "company_id": t.alice_co["id"], "name": "两人群",
        "agent_ids": [a["id"] for a in members]}, headers=t.alice["headers"])
    gid = r.json()["id"]

    # 没有模型密钥时 LLM 会失败，但端点仍应把两位成员都走一遍并落库
    await client.post(f"/group-chat/group/{gid}?company_id={t.alice_co['id']}",
                      json={"message": "在吗"}, headers=t.alice["headers"])
    r = await client.get(f"/group-chat/group/{gid}/history?company_id={t.alice_co['id']}",
                         headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert msgs[0]["role"] == "boss" and msgs[0]["text"] == "在吗"
    assert len(msgs) == 3
    assert {m["agent_name"] for m in msgs[1:]} == {a["name"] for a in members}


@pytest.mark.asyncio
async def test_group_chat_rejects_foreign_group_scope(client, tenants):
    t = tenants
    r = await client.post("/chat-groups", json={
        "company_id": t.bob_co["id"], "name": "B 的群",
        "agent_ids": [a["id"] for a in await _agents(client, t.bob_co["id"], t.bob, 1)]},
        headers=t.bob["headers"])
    gid = r.json()["id"]
    # A 拿着 B 的群 id 发消息：群成员解析为空，不应替 B 的成员发言
    await client.post(f"/group-chat/group/{gid}?company_id={t.alice_co['id']}",
                      json={"message": "越权测试"}, headers=t.alice["headers"])
    r = await client.get(f"/group-chat/group/{gid}/history?company_id={t.bob_co['id']}",
                         headers=t.bob["headers"])
    assert all(m["role"] == "boss" for m in r.json()["messages"])
