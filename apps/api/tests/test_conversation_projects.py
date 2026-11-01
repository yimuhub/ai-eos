"""C2 会话归属项目：创建/过滤/PATCH 与租户隔离。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _make_project(client, user, company_id: str, name: str = "项目") -> dict:
    r = await client.post("/projects", json={"company_id": company_id,
                                             "name": name},
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _make_conversation(client, user, company_id: str, *,
                             title: str = "讨论",
                             project_id: str | None = None) -> dict:
    payload = {"company_id": company_id, "title": title}
    if project_id is not None:
        payload["project_id"] = project_id
    r = await client.post("/conversations", json=payload,
                          headers=user["headers"])
    return r


async def test_create_conversation_with_project(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    project = await _make_project(client, alice, co["id"], "官网改版")

    r = await _make_conversation(client, alice, co["id"], title="改版讨论",
                                 project_id=project["id"])
    assert r.status_code == 201, r.text
    conv = r.json()
    assert conv["project_id"] == project["id"]
    assert conv["title"] == "改版讨论"

    r = await client.get(f"/conversations?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == conv["id"]
    assert rows[0]["project_id"] == project["id"], "列表导出必须含 project_id"


async def test_create_rejects_cross_company_project(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    bob_project = await _make_project(client, tenants.bob,
                                      tenants.bob_co["id"], "别人的项目")

    r = await _make_conversation(client, alice, co["id"],
                                 project_id=bob_project["id"])
    assert r.status_code == 404, "跨公司项目必须 404"

    r = await _make_conversation(client, alice, co["id"],
                                 project_id="does-not-exist")
    assert r.status_code == 404, "不存在的项目必须 404"


async def test_list_filters_by_project(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    p1 = await _make_project(client, alice, co["id"], "项目一")
    p2 = await _make_project(client, alice, co["id"], "项目二")

    c1 = (await _make_conversation(client, alice, co["id"], title="一",
                                   project_id=p1["id"])).json()
    c2 = (await _make_conversation(client, alice, co["id"], title="二",
                                   project_id=p2["id"])).json()
    c3 = (await _make_conversation(client, alice, co["id"], title="游离")).json()

    r = await client.get(
        f"/conversations?company_id={co['id']}&project_id={p1['id']}",
        headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()] == [c1["id"]]

    r = await client.get(f"/conversations?company_id={co['id']}",
                         headers=alice["headers"])
    assert {x["id"] for x in r.json()} == {c1["id"], c2["id"], c3["id"]}


async def test_patch_title_and_project(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    p1 = await _make_project(client, alice, co["id"], "项目一")
    p2 = await _make_project(client, alice, co["id"], "项目二")
    conv = (await _make_conversation(client, alice, co["id"], title="旧名",
                                     project_id=p1["id"])).json()

    r = await client.patch(f"/conversations/{conv['id']}",
                           json={"title": "新名"}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "新名"
    assert r.json()["project_id"] == p1["id"], "只改名不该动项目"

    r = await client.patch(f"/conversations/{conv['id']}",
                           json={"project_id": p2["id"]},
                           headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == p2["id"]
    assert r.json()["title"] == "新名"

    r = await client.patch(f"/conversations/{conv['id']}",
                           json={"project_id": ""}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] is None, "空字符串表示移出项目"

    bob_project = await _make_project(client, tenants.bob,
                                      tenants.bob_co["id"], "别人的项目")
    r = await client.patch(f"/conversations/{conv['id']}",
                           json={"project_id": bob_project["id"]},
                           headers=alice["headers"])
    assert r.status_code == 404, "跨公司项目必须 404"


async def test_cross_tenant_conversation_404(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    p = await _make_project(client, alice, co["id"], "私有项目")
    conv = (await _make_conversation(client, alice, co["id"],
                                     project_id=p["id"])).json()

    r = await client.get(f"/conversations?company_id={co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404

    r = await client.patch(f"/conversations/{conv['id']}",
                           json={"title": "越权改名"},
                           headers=tenants.bob["headers"])
    assert r.status_code == 404

    r = await client.get(f"/conversations/{conv['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404
