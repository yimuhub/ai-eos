"""B2 交付单：任务关联、验收/退回、交付评论与租户隔离。

全部走进程内 ASGI + conftest 的临时库，真实库（apps/api/data/aieos.db）
不会被打开；应用代码里自己 commit，因此这里不做事务回滚式隔离，
只依赖 conftest 的用例级清表。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact, ArtifactVersion
from app.models.deliverable import DeliverableComment
from app.models.notification import Notification
from app.models.task import Task

pytestmark = pytest.mark.asyncio


async def _seed_task(company_id: str, user_id: str, title: str = "写周报") -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title=title, created_by=user_id)
        s.add(t)
        await s.flush()
        task_id = t.id
        await s.commit()
    return task_id


async def _create_artifact(client, user, company_id: str, *, name="周报",
                           task_id: str | None = None) -> dict:
    payload = {"name": name, "artifact_type": "document", "content": "正文"}
    if task_id:
        payload["task_id"] = task_id
    r = await client.post(f"/artifacts?company_id={company_id}", json=payload,
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _approve(client, user, artifact_id: str) -> None:
    r = await client.post(f"/artifacts/{artifact_id}/submit",
                          json={"content": "新版正文", "change_note": "提交"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/artifacts/{artifact_id}/review",
                          json={"decision": "approve"}, headers=user["headers"])
    assert r.status_code == 200, r.text


async def test_create_artifact_with_task_and_filter(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    task_id = await _seed_task(co["id"], alice["id"])

    created = await _create_artifact(client, alice, co["id"], task_id=task_id)
    assert created["task_id"] == task_id

    detail = await client.get(f"/artifacts/{created['id']}",
                              headers=alice["headers"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["task_id"] == task_id
    assert body["accepted_by"] is None and body["accepted_at"] is None
    assert body["comment_count"] == 0

    listed = await client.get(
        f"/artifacts?company_id={co['id']}&task_id={task_id}",
        headers=alice["headers"])
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [r["id"] for r in rows] == [created["id"]]
    assert rows[0]["task_id"] == task_id
    assert "accepted_at" in rows[0]

    # 不带 task_id 过滤时也能看到（过滤是可选参数，不是默认排除）
    all_rows = (await client.get(f"/artifacts?company_id={co['id']}",
                                 headers=alice["headers"])).json()
    assert created["id"] in {r["id"] for r in all_rows}


async def test_create_with_foreign_or_missing_task_is_404(client, tenants):
    alice, bob = tenants.alice, tenants.bob
    bob_task = await _seed_task(tenants.bob_co["id"], bob["id"])

    r = await client.post(
        f"/artifacts?company_id={tenants.alice_co['id']}",
        json={"name": "偷挂任务", "task_id": bob_task},
        headers=alice["headers"])
    assert r.status_code == 404, "跨公司 task_id 必须 404，不能泄露任务是否存在"

    r = await client.post(
        f"/artifacts?company_id={tenants.alice_co['id']}",
        json={"name": "幽灵任务", "task_id": "does-not-exist"},
        headers=alice["headers"])
    assert r.status_code == 404


async def test_accept_requires_approved_and_is_idempotent(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co

    art = await _create_artifact(client, alice, co["id"], name="未过审交付物")
    r = await client.post(f"/artifacts/{art['id']}/accept",
                          headers=alice["headers"])
    assert r.status_code == 409, "DRAFT 不能验收"

    await _approve(client, alice, art["id"])

    # 造一个"别人创建"的成果，验证验收通知发给 created_by 而不是操作人。
    async with AsyncSessionLocal() as s:
        row = await s.get(Artifact, art["id"])
        row.created_by = bob["id"]
        await s.commit()

    r = await client.post(f"/artifacts/{art['id']}/accept",
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already"] is False
    assert body["accepted_by"] == alice["id"]
    assert body["accepted_at"] is not None

    r2 = await client.post(f"/artifacts/{art['id']}/accept",
                           headers=alice["headers"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["already"] is True
    assert r2.json()["accepted_at"] == body["accepted_at"]

    detail = (await client.get(f"/artifacts/{art['id']}",
                               headers=alice["headers"])).json()
    assert detail["accepted_by"] == alice["id"]
    assert detail["accepted_at"] == body["accepted_at"]

    async with AsyncSessionLocal() as s:
        notes = list((await s.execute(select(Notification).where(
            Notification.user_id == bob["id"]))).scalars().all())
    assert len(notes) == 1
    assert notes[0].title == f"交付物已验收：{art['name']}"
    assert notes[0].link == "/artifacts"


async def test_decline_reverts_to_rejected_and_allows_resubmit(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    art = await _create_artifact(client, alice, co["id"], name="有错的交付物")
    await _approve(client, alice, art["id"])

    # 先验收，再退回：accepted_* 必须被清空，不能留下"已验收但被打回"的矛盾状态
    r = await client.post(f"/artifacts/{art['id']}/accept",
                          headers=alice["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/artifacts/{art['id']}/decline",
                          json={"comment": "数据口径不对，请返工"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"

    detail = (await client.get(f"/artifacts/{art['id']}",
                               headers=alice["headers"])).json()
    assert detail["status"] == "REJECTED"
    assert detail["accepted_by"] is None
    assert detail["accepted_at"] is None
    assert detail["versions"][0]["review_comment"] == "数据口径不对，请返工"
    assert detail["comment_count"] == 1

    comments = (await client.get(f"/artifacts/{art['id']}/comments",
                                 headers=alice["headers"])).json()
    assert len(comments) == 1
    assert comments[0]["author_type"] == "user"
    assert comments[0]["author_id"] == alice["id"]
    assert comments[0]["body"] == "数据口径不对，请返工"

    # 退回后可以重新提交（REJECTED -> IN_REVIEW）
    r = await client.post(f"/artifacts/{art['id']}/submit",
                          json={"content": "返工后的正文", "change_note": "返工"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IN_REVIEW"

    # 非 APPROVED 状态下不能再次验收退回
    r = await client.post(f"/artifacts/{art['id']}/decline", json={},
                          headers=alice["headers"])
    assert r.status_code == 409

    # 超长意见按参数错误拒绝
    art2 = await _create_artifact(client, alice, co["id"], name="另一份")
    await _approve(client, alice, art2["id"])
    r = await client.post(f"/artifacts/{art2['id']}/decline",
                          json={"comment": "x" * 2001},
                          headers=alice["headers"])
    assert r.status_code == 400


async def test_comments_crud_and_tenant_isolation(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co
    art = await _create_artifact(client, alice, co["id"], name="待讨论")

    r = await client.post(f"/artifacts/{art['id']}/comments",
                          json={"body": "第一版先看结构"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["author_type"] == "user"
    assert first["author_id"] == alice["id"]

    r = await client.post(f"/artifacts/{art['id']}/comments",
                          json={"body": "再看数据"},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    second = r.json()

    rows = (await client.get(f"/artifacts/{art['id']}/comments",
                             headers=alice["headers"])).json()
    assert [c["id"] for c in rows] == [first["id"], second["id"]], "评论按时间正序"

    detail = (await client.get(f"/artifacts/{art['id']}",
                               headers=alice["headers"])).json()
    assert detail["comment_count"] == 2

    # 作者本人删除
    r = await client.delete(f"/artifacts/{art['id']}/comments/{first['id']}",
                            headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert (await client.get(f"/artifacts/{art['id']}/comments",
                             headers=alice["headers"])).json()[0]["id"] == second["id"]

    # 超长评论、空评论都按 400 拒绝
    r = await client.post(f"/artifacts/{art['id']}/comments",
                          json={"body": "x" * 4001},
                          headers=alice["headers"])
    assert r.status_code == 400
    r = await client.post(f"/artifacts/{art['id']}/comments",
                          json={"body": "   "},
                          headers=alice["headers"])
    assert r.status_code == 400

    # 外部租户：读、写、删全部 404（不泄露成果/评论存在性）
    r = await client.get(f"/artifacts/{art['id']}/comments",
                         headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post(f"/artifacts/{art['id']}/comments",
                          json={"body": "越权留言"}, headers=bob["headers"])
    assert r.status_code == 404
    r = await client.delete(f"/artifacts/{art['id']}/comments/{second['id']}",
                            headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post(f"/artifacts/{art['id']}/accept",
                          headers=bob["headers"])
    assert r.status_code == 404

    # 评论挂在别的成果下时也按不存在处理，不能凭 comment_id 跨成果删除
    other = await _create_artifact(client, alice, co["id"], name="另一份成果")
    r = await client.delete(f"/artifacts/{other['id']}/comments/{second['id']}",
                            headers=alice["headers"])
    assert r.status_code == 404


async def test_comment_delete_by_manager_and_data_scoping(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co
    art = await _create_artifact(client, alice, co["id"], name="管理删除")

    # 直接落一条由别人（非成员）写的评论：老板具备 MANAGE，应可删除
    async with AsyncSessionLocal() as s:
        c = DeliverableComment(company_id=co["id"], artifact_id=art["id"],
                               author_type="agent", author_id="agent-1",
                               body="Agent 的评论")
        s.add(c)
        await s.flush()
        comment_id = c.id
        await s.commit()

    r = await client.delete(f"/artifacts/{art['id']}/comments/{comment_id}",
                            headers=alice["headers"])
    assert r.status_code == 200, r.text

    async with AsyncSessionLocal() as s:
        assert await s.get(DeliverableComment, comment_id) is None

    # 非成员访问成果详情与评论列表都 404
    assert (await client.get(f"/artifacts/{art['id']}",
                             headers=bob["headers"])).status_code == 404
    assert (await client.get(f"/artifacts/{art['id']}/comments",
                             headers=bob["headers"])).status_code == 404

    # 成果本身仍可在本公司正常读取
    async with AsyncSessionLocal() as s:
        assert await s.get(Artifact, art["id"]) is not None
        versions = list((await s.execute(select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == art["id"]))).scalars().all())
        assert len(versions) == 1
