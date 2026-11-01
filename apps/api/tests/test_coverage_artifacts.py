"""覆盖率补齐：artifacts 成果协作全流程（版本/送审/审核/验收/评论/发布/下载）。

覆盖点：
* 创建、列表过滤（project/task/status）、详情、版本全文与打包下载
* 版本追加、DRAFT/REJECTED 的送审、审核状态机与非法流转 409
* 验收幂等、验收退回（清空验收痕迹 + 默认评论）、能力矩阵（member 不能删）
* 发布：无审批策略直接发布（幂等）与有策略时生成审批单再 decide
* SOP 交接单、删除成果、跨租户一律 404

全部走进程内 ASGI + 临时库，不触碰 apps/api/data/aieos.db。
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact, ArtifactVersion
from app.models.intervention import ApprovalRequest, InterventionPolicy
from app.models.membership import CompanyMember
from app.models.notification import Notification
from app.models.task import Task

pytestmark = pytest.mark.asyncio


async def _create(client, user, company_id: str, *, name: str = "成果",
                  content: str = "正文", **extra) -> dict:
    payload = {"name": name, "artifact_type": "document",
               "content": content, **extra}
    r = await client.post(f"/artifacts?company_id={company_id}", json=payload,
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _seed_task(company_id: str, user_id: str, title: str = "关联任务") -> str:
    async with AsyncSessionLocal() as s:
        t = Task(company_id=company_id, title=title, created_by=user_id)
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
    return tid


async def _approve(client, user, artifact_id: str, comment: str = "通过") -> dict:
    r = await client.post(f"/artifacts/{artifact_id}/submit",
                          json={"content": "提交审核版", "change_note": "提交"},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/artifacts/{artifact_id}/review",
                          json={"decision": "approve", "comment": comment},
                          headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_create_list_detail_version_and_zip(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co
    h = alice["headers"]
    r = await client.post("/projects", json={"name": "成果项目",
                                             "company_id": co["id"]}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    art = await _create(client, alice, co["id"], name="季度报告",
                        content="第一版正文", project_id=pid,
                        artifact_type="report")
    aid = art["id"]
    assert art["status"] == "DRAFT"
    assert art["current_version"] == 1
    assert art["task_id"] is None

    # 列表：全量 / 项目过滤 / 状态过滤 / 空结果
    rows = (await client.get(f"/artifacts?company_id={co['id']}",
                             headers=h)).json()
    assert [a["id"] for a in rows] == [aid]
    assert rows[0]["type"] == "report" and rows[0]["version"] == 1
    assert rows[0]["project_id"] == pid and rows[0]["accepted_at"] is None
    rows = (await client.get(
        f"/artifacts?company_id={co['id']}&project_id={pid}", headers=h)).json()
    assert [a["id"] for a in rows] == [aid]
    rows = (await client.get(
        f"/artifacts?company_id={co['id']}&project_id={uuid.uuid4().hex}",
        headers=h)).json()
    assert rows == []
    rows = (await client.get(
        f"/artifacts?company_id={co['id']}&status=DRAFT", headers=h)).json()
    assert [a["id"] for a in rows] == [aid]
    rows = (await client.get(
        f"/artifacts?company_id={co['id']}&status=APPROVED", headers=h)).json()
    assert rows == []
    # 非法状态枚举 → 参数校验失败
    r = await client.get(f"/artifacts?company_id={co['id']}&status=WHATEVER",
                         headers=h)
    assert r.status_code == 422

    # 详情：版本倒序、预览、评论数
    detail = (await client.get(f"/artifacts/{aid}", headers=h)).json()
    assert detail["name"] == "季度报告"
    assert detail["status"] == "DRAFT" and detail["current_version"] == 1
    assert detail["accepted_by"] is None and detail["comment_count"] == 0
    assert len(detail["versions"]) == 1
    assert detail["versions"][0]["version"] == 1
    assert detail["versions"][0]["status"] == "DRAFT"
    assert detail["versions"][0]["content_preview"] == "第一版正文"
    assert detail["versions"][0]["change_note"] == "初始版本"

    # 版本全文 + 不存在的版本
    r = await client.get(f"/artifacts/{aid}/versions/1", headers=h)
    assert r.status_code == 200
    assert r.json() == {"version": 1, "content": "第一版正文", "status": "DRAFT"}
    r = await client.get(f"/artifacts/{aid}/versions/99", headers=h)
    assert r.status_code == 404

    # 打包下载：v1.txt + meta.json
    r = await client.get(f"/artifacts/{aid}/download.zip", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert f'artifact-{aid}.zip' in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["meta.json", "v1.txt"]
    assert zf.read("v1.txt").decode() == "第一版正文"
    meta = json.loads(zf.read("meta.json"))
    assert meta == {"id": aid, "name": "季度报告", "type": "report",
                    "status": "DRAFT", "current_version": 1, "versions": 1}

    # 挂任务：只能挂本公司任务；列表支持 task_id 过滤
    tid = await _seed_task(co["id"], alice["id"])
    with_task = await _create(client, alice, co["id"], name="挂任务成果",
                              task_id=tid)
    assert with_task["task_id"] == tid
    rows = (await client.get(
        f"/artifacts?company_id={co['id']}&task_id={tid}", headers=h)).json()
    assert [a["id"] for a in rows] == [with_task["id"]]
    r = await client.post(f"/artifacts?company_id={co['id']}", json={
        "name": "幽灵任务", "task_id": uuid.uuid4().hex}, headers=h)
    assert r.status_code == 404
    bob_task = await _seed_task(tenants.bob_co["id"], bob["id"])
    r = await client.post(f"/artifacts?company_id={co['id']}", json={
        "name": "偷挂别家任务", "task_id": bob_task}, headers=h)
    assert r.status_code == 404

    # 跨租户：列表/详情/版本/zip/SOP 一律 404（不泄露存在性）
    b = bob["headers"]
    paths = [f"/artifacts?company_id={co['id']}",
             f"/artifacts/{aid}",
             f"/artifacts/{aid}/versions/1",
             f"/artifacts/{aid}/download.zip",
             f"/artifacts/{aid}/sop"]
    for path in paths:
        r = await client.get(path, headers=b)
        assert r.status_code == 404, f"{path} 泄露：{r.status_code} {r.text[:120]}"
    assert b"PK" not in (await client.get(
        f"/artifacts/{aid}/download.zip", headers=b)).content


async def test_version_submit_and_review_state_machine(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    h = alice["headers"]
    aid = (await _create(client, alice, co["id"], content="v1 正文"))["id"]

    # 草稿态追加版本：版本号取 max+1
    r = await client.post(f"/artifacts/{aid}/versions", json={
        "content": "v2 正文", "change_note": "补充数据",
        "submitted_by_agent_id": "agent-x"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"artifact_id": aid, "new_version": 2, "status": "DRAFT"}
    detail = (await client.get(f"/artifacts/{aid}", headers=h)).json()
    assert detail["current_version"] == 2
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["change_note"] == "补充数据"

    # 送审时带新内容 → 再追加一版
    r = await client.post(f"/artifacts/{aid}/submit", json={
        "content": "送审正文", "change_note": "待审"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IN_REVIEW"
    assert r.json()["current_version"] == 3

    # 非法决策 / IN_REVIEW 下不能送审或追加版本
    r = await client.post(f"/artifacts/{aid}/review",
                          json={"decision": "bogus"}, headers=h)
    assert r.status_code == 400
    assert "approve/reject/withdraw" in r.json()["detail"]
    r = await client.post(f"/artifacts/{aid}/submit",
                          json={"content": ""}, headers=h)
    assert r.status_code == 409
    r = await client.post(f"/artifacts/{aid}/versions",
                          json={"content": "偷加"}, headers=h)
    assert r.status_code == 409

    # 驳回：当前版本记录意见与审阅人，状态落到 REJECTED
    r = await client.post(f"/artifacts/{aid}/review", json={
        "decision": "reject", "comment": "论据不足",
        "reviewer_agent_id": "agent-reviewer"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"
    assert r.json()["reviewer"] == "agent-reviewer"
    detail = (await client.get(f"/artifacts/{aid}", headers=h)).json()
    assert detail["status"] == "REJECTED"
    assert detail["versions"][0]["version"] == 3
    assert detail["versions"][0]["status"] == "REJECTED"
    assert detail["versions"][0]["review_comment"] == "论据不足"
    assert detail["versions"][0]["reviewed_by"] == "agent-reviewer"

    # REJECTED 不能直接通过，只能重新送审
    r = await client.post(f"/artifacts/{aid}/review",
                          json={"decision": "approve"}, headers=h)
    assert r.status_code == 409

    # 返工后重新送审 → 撤回 → 再送审 → 通过
    r = await client.post(f"/artifacts/{aid}/submit", json={
        "content": "返工正文", "change_note": "返工"}, headers=h)
    assert r.status_code == 200 and r.json()["current_version"] == 4
    r = await client.post(f"/artifacts/{aid}/review",
                          json={"decision": "withdraw"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "DRAFT"
    r = await client.post(f"/artifacts/{aid}/submit",
                          json={"content": ""}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "IN_REVIEW"
    r = await client.post(f"/artifacts/{aid}/review", json={
        "decision": "approve", "reviewer_agent_id": "agent-boss"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert r.json()["reviewer"] == "agent-boss"

    # APPROVED 之后不能再送审 / 追加版本
    r = await client.post(f"/artifacts/{aid}/submit",
                          json={"content": ""}, headers=h)
    assert r.status_code == 409
    r = await client.post(f"/artifacts/{aid}/versions",
                          json={"content": "过期修改"}, headers=h)
    assert r.status_code == 409


async def test_accept_decline_flow_and_guards(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    h = alice["headers"]
    aid = (await _create(client, alice, co["id"], name="待验收"))["id"]

    # 未过审不能验收
    r = await client.post(f"/artifacts/{aid}/accept", headers=h)
    assert r.status_code == 409
    assert "APPROVED" in r.json()["detail"]

    await _approve(client, alice, aid, comment="可以接收")
    # 成果创建者不是操作人时，验收要给创建者发通知
    async with AsyncSessionLocal() as s:
        (await s.get(Artifact, aid)).created_by = tenants.bob["id"]
        await s.commit()
    r = await client.post(f"/artifacts/{aid}/accept", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already"] is False and body["status"] == "APPROVED"
    assert body["accepted_by"] == alice["id"]
    assert body["accepted_at"] is not None

    # 幂等：重复验收返回 already=true 且时间不变
    r2 = await client.post(f"/artifacts/{aid}/accept", headers=h)
    assert r2.status_code == 200
    assert r2.json()["already"] is True
    assert r2.json()["accepted_at"] == body["accepted_at"]
    async with AsyncSessionLocal() as s:
        notes = (await s.execute(select(Notification).where(
            Notification.user_id == tenants.bob["id"]))).scalars().all()
    assert len(notes) == 1
    assert notes[0].title == "交付物已验收：待验收"
    assert notes[0].link == "/artifacts"

    # 非 APPROVED 不能验收退回；超长意见 400
    draft = (await _create(client, alice, co["id"], name="草稿"))["id"]
    r = await client.post(f"/artifacts/{draft}/decline", json={}, headers=h)
    assert r.status_code == 409
    r = await client.post(f"/artifacts/{draft}/decline",
                          json={"comment": "x" * 2001}, headers=h)
    assert r.status_code == 400

    # 验收退回：状态 REJECTED、清空验收痕迹、意见写入当前版本与评论
    r = await client.post(f"/artifacts/{aid}/decline",
                          json={"comment": "数据要重算"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": aid, "status": "REJECTED",
                        "comment": "数据要重算"}
    detail = (await client.get(f"/artifacts/{aid}", headers=h)).json()
    assert detail["status"] == "REJECTED"
    assert detail["accepted_by"] is None and detail["accepted_at"] is None
    assert detail["versions"][0]["review_comment"] == "数据要重算"
    assert detail["comment_count"] == 1
    comments = (await client.get(f"/artifacts/{aid}/comments", headers=h)).json()
    assert comments[0]["body"] == "数据要重算"
    assert comments[0]["author_id"] == alice["id"]

    # 退回后不能再次验收，返工送审通过后可重新验收
    r = await client.post(f"/artifacts/{aid}/accept", headers=h)
    assert r.status_code == 409
    await _approve(client, alice, aid, comment="返工通过")
    r = await client.post(f"/artifacts/{aid}/accept", headers=h)
    assert r.status_code == 200 and r.json()["already"] is False

    # 不填意见时写入默认退回说明
    art2 = (await _create(client, alice, co["id"], name="无意见退回"))["id"]
    await _approve(client, alice, art2)
    r = await client.post(f"/artifacts/{art2}/decline", json={}, headers=h)
    assert r.status_code == 200 and r.json()["comment"] is None
    comments = (await client.get(f"/artifacts/{art2}/comments", headers=h)).json()
    assert comments[0]["body"] == "验收退回：请返工后重新提交"


async def test_comments_crud_and_member_capability(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co
    h = alice["headers"]
    aid = (await _create(client, alice, co["id"], name="待讨论"))["id"]

    # 空 / 超长评论按参数错误拒绝（在归属校验之前）
    r = await client.post(f"/artifacts/{aid}/comments",
                          json={"body": "   "}, headers=h)
    assert r.status_code == 400
    r = await client.post(f"/artifacts/{aid}/comments",
                          json={"body": "x" * 4001}, headers=h)
    assert r.status_code == 400

    r = await client.post(f"/artifacts/{aid}/comments",
                          json={"body": "先看结构"}, headers=h)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["author_type"] == "user" and first["author_id"] == alice["id"]
    second = (await client.post(f"/artifacts/{aid}/comments",
                                json={"body": "再看数据"},
                                headers=h)).json()

    rows = (await client.get(f"/artifacts/{aid}/comments", headers=h)).json()
    assert [c["id"] for c in rows] == [first["id"], second["id"]]
    assert (await client.get(f"/artifacts/{aid}",
                             headers=h)).json()["comment_count"] == 2

    # 作者本人可删；删不存在的评论 404
    r = await client.delete(f"/artifacts/{aid}/comments/{first['id']}", headers=h)
    assert r.status_code == 200 and r.json()["deleted"] is True
    r = await client.delete(f"/artifacts/{aid}/comments/{first['id']}", headers=h)
    assert r.status_code == 404

    # 评论挂在别的成果下 → 不能凭 comment_id 跨成果删除
    other = (await _create(client, alice, co["id"], name="另一份"))["id"]
    r = await client.delete(f"/artifacts/{other}/comments/{second['id']}", headers=h)
    assert r.status_code == 404

    # 陌生人（非成员）读写删全部 404
    b = bob["headers"]
    assert (await client.get(f"/artifacts/{aid}/comments",
                             headers=b)).status_code == 404
    assert (await client.post(f"/artifacts/{aid}/comments",
                              json={"body": "越权"},
                              headers=b)).status_code == 404
    assert (await client.delete(f"/artifacts/{aid}/comments/{second['id']}",
                                headers=b)).status_code == 404

    # bob 加入公司成为普通成员：可读可评论（OPERATE），但不能删他人评论/删成果（需 MANAGE）
    async with AsyncSessionLocal() as s:
        s.add(CompanyMember(company_id=co["id"], user_id=bob["id"],
                            role="member", status="active"))
        await s.commit()
    assert (await client.get(f"/artifacts/{aid}", headers=b)).status_code == 200
    r = await client.post(f"/artifacts/{aid}/comments",
                          json={"body": "成员补充"}, headers=b)
    assert r.status_code == 200 and r.json()["author_id"] == bob["id"]
    bob_comment = r.json()["id"]
    r = await client.delete(f"/artifacts/{aid}/comments/{second['id']}", headers=b)
    assert r.status_code == 404
    r = await client.delete(f"/artifacts/{aid}", headers=b)
    assert r.status_code == 404

    # 业主具备 MANAGE：可删成员评论；成员可删自己的评论
    r = await client.delete(f"/artifacts/{aid}/comments/{bob_comment}", headers=h)
    assert r.status_code == 200
    r = await client.post(f"/artifacts/{aid}/comments",
                          json={"body": "自己的话"}, headers=b)
    own = r.json()["id"]
    assert (await client.delete(f"/artifacts/{aid}/comments/{own}",
                                headers=b)).status_code == 200


async def test_publish_direct_idempotent_and_guard(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    h = alice["headers"]
    aid = (await _create(client, alice, co["id"], name="对外交付"))["id"]

    # 未过审不能发布
    r = await client.post(f"/artifacts/{aid}/publish", headers=h)
    assert r.status_code == 409
    assert "APPROVED" in r.json()["detail"]

    await _approve(client, alice, aid)
    r = await client.post(f"/artifacts/{aid}/publish", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["published_at"] and "已发布" in body["message"]
    async with AsyncSessionLocal() as s:
        assert (await s.get(Artifact, aid)).published_at is not None

    # 已发布 → 幂等
    r2 = await client.post(f"/artifacts/{aid}/publish", headers=h)
    assert r2.status_code == 200
    assert r2.json()["already"] is True
    assert r2.json()["published_at"] == body["published_at"]

    # 不存在的成果 404；跨租户 404
    r = await client.post(f"/artifacts/{uuid.uuid4().hex}/publish", headers=h)
    assert r.status_code == 404
    r = await client.post(f"/artifacts/{aid}/publish",
                          headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_publish_approval_policy_and_decide(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    h = alice["headers"]
    async with AsyncSessionLocal() as s:
        s.add(InterventionPolicy(id=str(uuid.uuid4()), company_id=co["id"],
                                 level=1,
                                 approval_actions=["artifact_publish"]))
        await s.commit()

    aid = (await _create(client, alice, co["id"], name="需二次确认"))["id"]
    await _approve(client, alice, aid)

    r = await client.post(f"/artifacts/{aid}/publish", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    assert body["approval_id"]
    async with AsyncSessionLocal() as s:
        req = await s.get(ApprovalRequest, body["approval_id"])
        assert req is not None
        assert req.company_id == co["id"]
        assert req.action_type == "artifact_publish"
        assert req.status == "PENDING"
        assert req.payload == {"artifact_id": aid, "awaiting_execution": True}
        assert "需二次确认" in req.reason
        assert (await s.get(Artifact, aid)).published_at is None

    # 非法决策 400
    r = await client.post(f"/artifacts/{aid}/publish/decide",
                          json={"decision": "maybe"}, headers=h)
    assert r.status_code == 400

    # 批准 → 审批单 APPROVED、成果标记已发布
    r = await client.post(f"/artifacts/{aid}/publish/decide",
                          json={"decision": "approve"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": aid, "decision": "approve", "published": True}
    async with AsyncSessionLocal() as s:
        req = await s.get(ApprovalRequest, body["approval_id"])
        assert req.status == "APPROVED" and req.decided_by == alice["id"]
        assert (await s.get(Artifact, aid)).published_at is not None

    # 没有待审批单 → 404
    r = await client.post(f"/artifacts/{aid}/publish/decide",
                          json={"decision": "approve"}, headers=h)
    assert r.status_code == 404

    # 第二份：驳回不标记发布时间
    art2 = (await _create(client, alice, co["id"], name="被驳回发布"))["id"]
    await _approve(client, alice, art2)
    pending = (await client.post(f"/artifacts/{art2}/publish", headers=h)).json()
    assert pending["status"] == "pending_approval"
    r = await client.post(f"/artifacts/{art2}/publish/decide",
                          json={"decision": "reject"}, headers=h)
    assert r.status_code == 200
    assert r.json()["published"] is False
    async with AsyncSessionLocal() as s:
        assert (await s.get(ApprovalRequest, pending["approval_id"])).status == \
            "REJECTED"
        assert (await s.get(Artifact, art2)).published_at is None

    # 读取审批策略失败时必须 fail-closed：宁可多一道确认，不能静默放行
    import app.api.routes.artifacts as ar
    art4 = (await _create(client, alice, co["id"], name="策略读不到"))["id"]
    await _approve(client, alice, art4)
    real_select = ar.select

    def _db_down(*args, **kwargs):
        raise RuntimeError("policy store down")

    monkeypatch.setattr(ar, "select", _db_down)
    r = await client.post(f"/artifacts/{art4}/publish", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    monkeypatch.setattr(ar, "select", real_select)
    fallback = r.json()["approval_id"]
    async with AsyncSessionLocal() as s:
        assert (await s.get(ApprovalRequest, fallback)).status == "PENDING"
        assert (await s.get(Artifact, art4)).published_at is None
    r = await client.post(f"/artifacts/{art4}/publish/decide",
                          json={"decision": "approve"}, headers=h)
    assert r.status_code == 200 and r.json()["published"] is True

    # 策略移除 artifact_publish → 直接发布
    async with AsyncSessionLocal() as s:
        pol = (await s.execute(select(InterventionPolicy).where(
            InterventionPolicy.company_id == co["id"]))).scalars().first()
        pol.approval_actions = ["task_start"]
        await s.commit()
    art3 = (await _create(client, alice, co["id"], name="直接发布"))["id"]
    await _approve(client, alice, art3)
    r = await client.post(f"/artifacts/{art3}/publish", headers=h)
    assert r.status_code == 200
    assert "已发布" in r.json()["message"]


async def test_sop_markdown_and_delete(client, tenants):
    alice, bob, co = tenants.alice, tenants.bob, tenants.alice_co
    h = alice["headers"]
    aid = (await _create(client, alice, co["id"], name="交接成果",
                         content="第一版交接内容"))["id"]

    # 草稿 SOP：包含状态与下一步建议
    r = await client.get(f"/artifacts/{aid}/sop", headers=h)
    assert r.status_code == 200, r.text
    sop = r.json()
    assert sop["artifact_id"] == aid and sop["title"] == "交接成果"
    assert sop["versions"] == 1
    md = sop["markdown"]
    assert md.startswith("# 交接单：交接成果")
    assert "- **状态**：DRAFT（v1）" in md
    assert "第一版交接内容" in md
    assert "暂无" in md
    assert "完善内容后提交审阅" in md

    # IN_REVIEW / REJECTED 的下一步建议分支
    art2 = (await _create(client, alice, co["id"], name="审阅中"))["id"]
    r = await client.post(f"/artifacts/{art2}/submit",
                          json={"content": ""}, headers=h)
    assert r.status_code == 200
    md2 = (await client.get(f"/artifacts/{art2}/sop", headers=h)).json()["markdown"]
    assert "等待/推进审阅决策" in md2
    r = await client.post(f"/artifacts/{art2}/review",
                          json={"decision": "reject", "comment": "再改改"},
                          headers=h)
    assert r.status_code == 200
    md3 = (await client.get(f"/artifacts/{art2}/sop", headers=h)).json()["markdown"]
    assert "按审阅意见修订后重新提交" in md3

    # 通过后 SOP 记录审阅意见与发布建议
    await _approve(client, alice, aid, comment="结构清晰")
    sop = (await client.get(f"/artifacts/{aid}/sop", headers=h)).json()
    assert sop["versions"] == 2
    assert "- **状态**：APPROVED（v2）" in sop["markdown"]
    assert "结构清晰" in sop["markdown"]
    assert "可发布" in sop["markdown"]

    # 普通成员不能删除成果（需 MANAGE），业主可以
    async with AsyncSessionLocal() as s:
        s.add(CompanyMember(company_id=co["id"], user_id=bob["id"],
                            role="member", status="active"))
        await s.commit()
    r = await client.delete(f"/artifacts/{aid}", headers=bob["headers"])
    assert r.status_code == 404

    r = await client.delete(f"/artifacts/{aid}", headers=h)
    assert r.status_code == 200 and r.json() == {"id": aid, "deleted": True}
    r = await client.get(f"/artifacts/{aid}", headers=h)
    assert r.status_code == 404
    async with AsyncSessionLocal() as s:
        assert await s.get(Artifact, aid) is None
        rows = (await s.execute(select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == aid))).scalars().all()
        assert rows == []
    r = await client.delete(f"/artifacts/{aid}", headers=h)
    assert r.status_code == 404
