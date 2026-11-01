"""站内通知（C4）：完成 / 失败 / 待审批三类事件必达。

测试原则沿用仓库既有约定：真实 ASGI + 真实鉴权 + conftest 临时 SQLite，
不 mock 依赖。通知的租户边界（他人 / 他司）是重点，也是这类"提示型"
功能最容易漏掉的越权面。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.meeting import Meeting, MeetingStatus
from app.models.membership import CompanyMember
from app.models.notification import Notification
from app.models.task import Task, TaskStatus
from app.services.notification_service import notify
from app.task_engine import transition

ENSURE_URL = "/chat-groups/preset/marketing/ensure"


async def _set_owner_quota(user_id: str, company_id: str, quota: int) -> None:
    """改 owner 的个人配额（_resolve_agent_quota 的第一优先级）。"""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(CompanyMember).where(
            CompanyMember.company_id == company_id,
            CompanyMember.user_id == user_id))).scalars().first()
        assert row is not None, "建公司时应写入 owner 的成员镜像行"
        row.agent_quota = quota
        await s.commit()


async def _seed(user_id: str, company_id: str, *, title: str = "通知",
                type: str = "test.event", body: str | None = None,
                link: str | None = "/tasks") -> str:
    """直接调用服务落一条通知，返回 id。"""
    async with AsyncSessionLocal() as s:
        row = await notify(s, user_id=user_id, company_id=company_id,
                           type=type, title=title, body=body, link=link)
        await s.commit()
        return row.id


async def _all_notifications(company_id: str) -> list[Notification]:
    async with AsyncSessionLocal() as s:
        return list((await s.execute(select(Notification).where(
            Notification.company_id == company_id))).scalars().all())


async def _create_task(client, user: dict, company_id: str,
                       title: str = "写周报") -> dict:
    r = await client.post("/tasks", json={"company_id": company_id,
                                          "title": title},
                          headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _list(client, user: dict, company_id: str, query: str = "") -> list[dict]:
    r = await client.get(f"/notifications?company_id={company_id}{query}",
                         headers=user["headers"])
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------- 事件埋点

async def test_agent_quota_approval_writes_pending_notification(client, tenants):
    """审批单落库的同一次提交里，owner 必须收到「待审批」。"""
    t = tenants
    cid = t.alice_co["id"]
    await _set_owner_quota(t.alice["id"], cid, 2)

    r = await client.post(ENSURE_URL, json={"company_id": cid},
                          headers=t.alice["headers"])
    assert r.status_code == 409, r.text

    rows = await _all_notifications(cid)
    assert len(rows) == 1, f"应只有一条待审批通知，实际 {len(rows)}"
    n = rows[0]
    assert n.user_id == t.alice["id"]
    assert n.type == "approval.pending"
    assert n.link == "/intervention"
    assert n.is_read is False
    assert "待审批" in n.title


async def test_task_completion_notifies_creator(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    task = await _create_task(client, t.alice, cid, title="整理会议纪要")

    async with AsyncSessionLocal() as s:
        row = await s.get(Task, task["id"])
        await transition(s, row, TaskStatus.RUNNING)
        await transition(s, row, TaskStatus.COMPLETED)

    items = await _list(client, t.alice, cid)
    assert len(items) == 1, f"完成只应通知一次，实际 {items}"
    assert items[0]["type"] == "task.completed"
    assert items[0]["link"] == f"/tasks/{task['id']}"
    assert "整理会议纪要" in items[0]["title"]
    assert items[0]["is_read"] is False


async def test_task_failure_notifies_creator(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    task = await _create_task(client, t.alice, cid, title="拉取销售数据")

    async with AsyncSessionLocal() as s:
        row = await s.get(Task, task["id"])
        await transition(s, row, TaskStatus.RUNNING)
        await transition(s, row, TaskStatus.FAILED)

    items = await _list(client, t.alice, cid)
    assert len(items) == 1
    assert items[0]["type"] == "task.failed"
    assert items[0]["link"] == f"/tasks/{task['id']}"


async def test_meeting_minutes_notifies_creator(client, tenants):
    """纪要生成与通知同事务（幂等占位符保证只发一次）。"""
    from app.meeting_engine import _finalize

    t = tenants
    cid = t.alice_co["id"]
    async with AsyncSessionLocal() as s:
        m = Meeting(company_id=cid, title="季度复盘会",
                    created_by=t.alice["id"], status=MeetingStatus.RUNNING)
        s.add(m)
        await s.commit()
        await s.refresh(m)
        await _finalize(s, m, [])

    rows = await _all_notifications(cid)
    assert len(rows) == 1
    assert rows[0].user_id == t.alice["id"]
    assert rows[0].type == "meeting.minutes"
    assert rows[0].link == "/meetings"
    assert "季度复盘会" in rows[0].title


# ---------------------------------------------------------------- 列表隔离

async def test_list_isolated_by_user(client, tenants):
    """同一公司内 A 只看到自己的通知，B 的不出现。"""
    t = tenants
    cid = t.alice_co["id"]
    await _seed(t.alice["id"], cid, title="给 alice")
    await _seed(t.bob["id"], cid, title="给 bob")

    titles = [n["title"] for n in await _list(client, t.alice, cid)]
    assert titles == ["给 alice"]


async def test_list_isolated_by_company(client, tenants, make_company):
    """跨公司：B 读 A 公司的通知 → 404；A 切到自己的另一家公司不串号。"""
    t = tenants
    a_cid = t.alice_co["id"]
    b_cid = (await make_company(t.alice, name="A 的第二家公司"))["id"]
    await _seed(t.alice["id"], a_cid, title="A 公司的事")

    assert await _list(client, t.alice, b_cid) == []
    r = await client.get(f"/notifications?company_id={a_cid}",
                         headers=t.bob["headers"])
    assert r.status_code == 404, "他人公司通知列表必须 404"


# ---------------------------------------------------------------- 未读与已读

async def test_unread_count_and_mark_read(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    n1 = await _seed(t.alice["id"], cid, title="一")
    await _seed(t.alice["id"], cid, title="二")
    await _seed(t.alice["id"], cid, title="三")

    async def count() -> int:
        r = await client.get(f"/notifications/unread-count?company_id={cid}",
                             headers=t.alice["headers"])
        assert r.status_code == 200, r.text
        return r.json()["count"]

    assert await count() == 3

    r = await client.post(f"/notifications/{n1}/read", headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["is_read"] is True
    assert await count() == 2

    # 重复标记幂等
    r = await client.post(f"/notifications/{n1}/read", headers=t.alice["headers"])
    assert r.status_code == 200
    assert await count() == 2


async def test_cannot_mark_other_users_notification(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    nid = await _seed(t.alice["id"], cid, title="alice 的私信")

    r = await client.post(f"/notifications/{nid}/read",
                          headers=t.bob["headers"])
    assert r.status_code == 404, "他人通知必须 404"

    async with AsyncSessionLocal() as s:
        row = await s.get(Notification, nid)
        assert row.is_read is False, "越权标记竟然生效了"


async def test_cannot_mark_notification_of_foreign_company(client, tenants):
    """通知属于自己、但公司与自己无关（脏数据/被移出公司）→ 404。"""
    t = tenants
    nid = await _seed(t.alice["id"], t.bob_co["id"], title="B 公司的通知")

    r = await client.post(f"/notifications/{nid}/read",
                          headers=t.alice["headers"])
    assert r.status_code == 404


async def test_read_all_only_affects_own_and_current_company(client, tenants,
                                                             make_company):
    t = tenants
    a_cid = t.alice_co["id"]
    b_cid = (await make_company(t.alice, name="另一家公司"))["id"]
    await _seed(t.alice["id"], a_cid, title="A-1")
    await _seed(t.alice["id"], a_cid, title="A-2")
    await _seed(t.alice["id"], b_cid, title="B-1")
    bob_nid = await _seed(t.bob["id"], t.bob_co["id"], title="bob 的")

    r = await client.post(f"/notifications/read-all?company_id={a_cid}",
                          headers=t.alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 2

    async with AsyncSessionLocal() as s:
        rows = list((await s.execute(select(Notification))).scalars().all())
        by_title = {x.title: x for x in rows}
        bob_row = await s.get(Notification, bob_nid)
    assert by_title["A-1"].is_read is True
    assert by_title["A-2"].is_read is True
    assert by_title["B-1"].is_read is False, "全部已读不能跨公司"
    assert by_title["bob 的"].is_read is False, "全部已读不能影响他人"
    assert bob_row.is_read is False


# ---------------------------------------------------------------- 参数与截断

async def test_limit_default_and_cap(client, tenants):
    t = tenants
    cid = t.alice_co["id"]
    for i in range(6):
        await _seed(t.alice["id"], cid, title=f"通知 {i}")

    assert len(await _list(client, t.alice, cid)) == 6  # 默认 20，够放
    assert len(await _list(client, t.alice, cid, "&limit=3")) == 3

    r = await client.get(f"/notifications?company_id={cid}&limit=51",
                         headers=t.alice["headers"])
    assert r.status_code == 422, "limit 超过 50 必须被拒绝"


async def test_long_title_and_body_are_truncated(client, tenants):
    """异常信息/Agent 输出再长也不能撑爆表，更不能让业务事务失败。"""
    t = tenants
    cid = t.alice_co["id"]
    await _seed(t.alice["id"], cid, title="T" * 500, body="B" * 5000)

    items = await _list(client, t.alice, cid)
    assert len(items) == 1
    assert len(items[0]["title"]) == 200
    assert len(items[0]["body"]) == 2000
