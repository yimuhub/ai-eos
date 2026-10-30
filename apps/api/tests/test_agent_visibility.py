"""P5：Agent 可见性（读）与创作权（写）+ 配额审批。

## 这一组钉住什么

多成员之后，"谁能看到哪个数字员工"是**行级**问题，不再是"是不是公司所有者"：

* owner / company_admin：全公司；
* 部门负责人：本部门 + 公司公开 + 自建；
* 普通成员：本部门公开 + 公司公开 + 自建。

列表与单行入口（详情/更新/删除/对话）必须用**同一套条件**——否则会出现
"列表里看不到、拿 id 直连却能聊"的经典漏洞。因此本文件的负向用例统一
断言 **404**（不是 403）：403 等于告诉调用方"这个 id 存在"。

配额口径：`company_members.agent_quota`（本人）→ `departments.agent_quota`
（目标部门）→ 公司默认（当前无此列 = 不限），取第一个非 None 的值；
超限不硬失败，而是落一条 `ApprovalRequest` 并 409。

数据库一律走 conftest 的 tmp sqlite（`AsyncSessionLocal`），不碰 data/aieos.db。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.company import Department
from app.models.intervention import ApprovalRequest
from app.models.membership import CompanyMember, DepartmentMember


# ---------------------------------------------------------------------------
# 夹具：一家公司 + A/B 两个部门 + 两个部门负责人 + 一个普通成员 + owner
# ---------------------------------------------------------------------------

async def _mk_dept(client, owner: dict, cid: str, name: str,
                   quota: int | None = None) -> dict:
    r = await client.post("/departments", params={"cid": cid},
                          json={"name": name}, headers=owner["headers"])
    assert r.status_code == 201, r.text
    dept = r.json()
    if quota is not None:
        async with AsyncSessionLocal() as s:
            row = await s.get(Department, dept["id"])
            row.agent_quota = quota
            await s.commit()
    return dept


async def _set_company_member_quota(user_id: str, company_id: str,
                                    quota: int | None) -> None:
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(CompanyMember).where(
            CompanyMember.company_id == company_id,
            CompanyMember.user_id == user_id))).scalars().first()
        row.agent_quota = quota
        await s.commit()


@pytest.fixture
async def org(client, make_user, make_company) -> SimpleNamespace:
    owner = await make_user()
    head_a = await make_user()
    head_b = await make_user()
    member = await make_user()
    company = await make_company(owner, name="P5 可见性公司")
    cid = company["id"]

    dept_a = await _mk_dept(client, owner, cid, "A 部")
    dept_b = await _mk_dept(client, owner, cid, "B 部")

    # 成员关系直接写库（界面尚未提供成员管理入口）：head 只靠部门成员行表达，
    # member 同时有公司成员行（active）与部门成员行。
    async with AsyncSessionLocal() as s:
        s.add(CompanyMember(company_id=cid, user_id=member["id"],
                            role="member", status="active"))
        s.add(DepartmentMember(department_id=dept_a["id"],
                               user_id=member["id"], role_in_dept="member"))
        s.add(DepartmentMember(department_id=dept_a["id"],
                               user_id=head_a["id"], role_in_dept="head"))
        s.add(DepartmentMember(department_id=dept_b["id"],
                               user_id=head_b["id"], role_in_dept="head"))
        await s.commit()

    return SimpleNamespace(owner=owner, head_a=head_a, head_b=head_b,
                           member=member, company=company, cid=cid,
                           dept_a=dept_a, dept_b=dept_b)


async def _create_agent(client, actor: dict, cid: str, name: str, *,
                        dept: str | None = None,
                        visibility: str | None = None):
    body: dict = {"company_id": cid, "name": name}
    if dept:
        body["department_id"] = dept
    if visibility:
        body["visibility"] = visibility
    return await client.post("/agents", json=body, headers=actor["headers"])


async def _agent_row(agent_id: str) -> Agent:
    async with AsyncSessionLocal() as s:
        row = await s.get(Agent, agent_id)
        assert row is not None
        return row


async def _list_ids(client, actor: dict, **params) -> set[str]:
    r = await client.get("/agents", params=params, headers=actor["headers"])
    assert r.status_code == 200, r.text
    return {a["id"] for a in r.json()}


# ---------------------------------------------------------------------------
# 读：可见范围
# ---------------------------------------------------------------------------

async def test_owner_sees_every_agent_including_other_departments(client, org):
    a_agent = await _create_agent(client, org.head_a, org.cid, "A 部员工",
                                  dept=org.dept_a["id"])
    b_agent = await _create_agent(client, org.head_b, org.cid, "B 部私有",
                                  dept=org.dept_b["id"], visibility="private")
    assert a_agent.status_code == 201 and b_agent.status_code == 201

    ids = await _list_ids(client, org.owner, company_id=org.cid)
    assert {a_agent.json()["id"], b_agent.json()["id"]} <= ids
    # 详情也看得到（列表与单行同源）
    for aid in (a_agent.json()["id"], b_agent.json()["id"]):
        r = await client.get(f"/agents/{aid}", headers=org.owner["headers"])
        assert r.status_code == 200


async def test_head_cannot_see_other_department_private_agent(client, org):
    """A 负责人拿 B 部门私有 Agent 的 id：详情/更新/对话/删除全部 404。"""
    created = await _create_agent(client, org.head_b, org.cid, "B 部私有",
                                  dept=org.dept_b["id"], visibility="private")
    assert created.status_code == 201
    bid = created.json()["id"]

    assert bid not in await _list_ids(client, org.head_a, company_id=org.cid)

    h = org.head_a["headers"]
    assert (await client.get(f"/agents/{bid}", headers=h)).status_code == 404
    assert (await client.patch(f"/agents/{bid}", json={"name": "改名"},
                               headers=h)).status_code == 404
    assert (await client.post(f"/agents/{bid}/chat",
                              json={"message": "在吗"}, headers=h)).status_code == 404
    assert (await client.delete(f"/agents/{bid}", headers=h)).status_code == 404

    # 没有被偷偷改掉
    assert (await _agent_row(bid)).name == "B 部私有"


async def test_member_sees_only_own_private_and_own_department(client, org):
    m = org.member["headers"]
    own = await _create_agent(client, org.member, org.cid, "我的私有",
                              dept=org.dept_a["id"], visibility="private")
    dept_agent = await _create_agent(client, org.head_a, org.cid, "A 部公开",
                                     dept=org.dept_a["id"], visibility="department")
    other = await _create_agent(client, org.head_b, org.cid, "B 部公开",
                                dept=org.dept_b["id"], visibility="department")
    for r in (own, dept_agent, other):
        assert r.status_code == 201, r.text

    ids = await _list_ids(client, org.member, company_id=org.cid)
    assert ids == {own.json()["id"], dept_agent.json()["id"]}

    # 单行入口与列表一致
    assert (await client.get(f"/agents/{other.json()['id']}",
                             headers=m)).status_code == 404
    assert (await client.get(f"/agents/{dept_agent.json()['id']}",
                             headers=m)).status_code == 200


async def test_company_visible_agent_reaches_every_member(client, org):
    created = await _create_agent(client, org.owner, org.cid, "全公司前台",
                                  dept=org.dept_a["id"], visibility="company")
    assert created.status_code == 201
    aid = created.json()["id"]

    for actor in (org.member, org.head_a, org.head_b):
        assert aid in await _list_ids(client, actor, company_id=org.cid)
        assert (await client.get(f"/agents/{aid}",
                                 headers=actor["headers"])).status_code == 200


async def test_member_cannot_read_other_company_agent(client, make_user, make_company, org):
    outsider = await make_user()
    other_co = await make_company(outsider, name="别家公司")
    created = await _create_agent(client, outsider, other_co["id"],
                                  "别人家的员工", visibility="company")
    assert created.status_code == 201
    aid = created.json()["id"]

    assert (await client.get(f"/agents/{aid}",
                             headers=org.member["headers"])).status_code == 404
    # 指定别家公司作为过滤条件同样 404（不泄露该公司是否存在）
    assert (await client.get("/agents", params={"company_id": other_co["id"]},
                             headers=org.member["headers"])).status_code == 404


# ---------------------------------------------------------------------------
# 写：创作权
# ---------------------------------------------------------------------------

async def test_head_creates_department_agent_with_creator(client, org):
    """负责人在本部门建 Agent：默认 visibility=department，归属写对自己。"""
    r = await _create_agent(client, org.head_a, org.cid, "A 部新员工",
                            dept=org.dept_a["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["visibility"] == "department"
    assert body["created_by_user_id"] == org.head_a["id"]

    row = await _agent_row(body["id"])
    assert row.created_by_user_id == org.head_a["id"]
    assert row.department_id == org.dept_a["id"]


async def test_member_and_head_cannot_publish_company_visibility(client, org):
    r_member = await _create_agent(client, org.member, org.cid, "成员想广播",
                                   dept=org.dept_a["id"], visibility="company")
    assert r_member.status_code == 404
    r_head = await _create_agent(client, org.head_a, org.cid, "负责人想广播",
                                 dept=org.dept_a["id"], visibility="company")
    assert r_head.status_code == 404

    # 确实没落库
    assert await _list_ids(client, org.member, company_id=org.cid) == set()


async def test_invalid_visibility_is_rejected_with_422(client, org):
    r = await _create_agent(client, org.owner, org.cid, "非法可见性",
                            visibility="team")
    assert r.status_code == 422


async def test_member_cannot_delete_or_pause_department_agent(client, org):
    created = await _create_agent(client, org.head_a, org.cid, "A 部员工",
                                  dept=org.dept_a["id"], visibility="department")
    assert created.status_code == 201
    aid = created.json()["id"]
    m = org.member["headers"]

    # 看得到，但删不掉、停不了（DESTRUCTIVE / MANAGE 不在 member 矩阵里）
    assert (await client.get(f"/agents/{aid}", headers=m)).status_code == 200
    assert (await client.delete(f"/agents/{aid}", headers=m)).status_code == 404
    assert (await client.patch(f"/agents/{aid}", json={"status": "DISABLED"},
                               headers=m)).status_code == 404
    row = await _agent_row(aid)
    assert row is not None and str(row.status.value) != "DISABLED"


async def test_member_can_edit_own_agent(client, org):
    own = await _create_agent(client, org.member, org.cid, "我的私有",
                              dept=org.dept_a["id"], visibility="private")
    assert own.status_code == 201
    aid = own.json()["id"]
    r = await client.patch(f"/agents/{aid}", json={"name": "我的私有 v2"},
                           headers=org.member["headers"])
    assert r.status_code == 200, r.text
    assert (await _agent_row(aid)).name == "我的私有 v2"


# ---------------------------------------------------------------------------
# 对话入口：可见性门禁 + 流式不回退成 SSE 错误
# ---------------------------------------------------------------------------

async def test_chat_history_allowed_for_visible_agent(client, org):
    created = await _create_agent(client, org.head_a, org.cid, "A 部员工",
                                  dept=org.dept_a["id"], visibility="department")
    aid = created.json()["id"]
    r = await client.get(f"/agents/{aid}/chat/history",
                         headers=org.member["headers"])
    assert r.status_code == 200
    assert r.json() == []


async def test_chat_stream_returns_404_not_sse_for_hidden_agent(client, org):
    """流式接口的越权必须是真的 404——不能开流后塞一条 error 文本。"""
    created = await _create_agent(client, org.head_b, org.cid, "B 部私有",
                                  dept=org.dept_b["id"], visibility="private")
    bid = created.json()["id"]
    r = await client.post(f"/agents/{bid}/chat/stream",
                          json={"message": "在吗"},
                          headers=org.head_a["headers"])
    assert r.status_code == 404
    assert "text/event-stream" not in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 配额：优先级 + 审批单
# ---------------------------------------------------------------------------

async def test_quota_exceeded_creates_approval_request(client, org):
    async with AsyncSessionLocal() as s:
        dept = await s.get(Department, org.dept_a["id"])
        dept.agent_quota = 1
        await s.commit()

    first = await _create_agent(client, org.owner, org.cid, "占位员工",
                                dept=org.dept_a["id"])
    assert first.status_code == 201, first.text

    second = await _create_agent(client, org.owner, org.cid, "超额员工",
                                 dept=org.dept_a["id"])
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert "超出 1" in detail and "审批" in detail

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.company_id == org.cid))).scalars().all()
    assert len(rows) == 1
    req = rows[0]
    assert req.action_type == "agent_quota"
    assert req.status == "PENDING"
    assert req.payload["quota"] == 1
    assert req.payload["overage"] == 1
    assert req.payload["requested_by_user_id"] == org.owner["id"]
    assert req.payload["department_id"] == org.dept_a["id"]

    # 超额的那个确实没被创建
    assert len(await _list_ids(client, org.owner, company_id=org.cid)) == 1


async def test_personal_quota_takes_priority_over_department_quota(client, org):
    """本人 1 < 部门 5：先卡本人的 1，删掉本人配额后部门 5 才生效。"""
    async with AsyncSessionLocal() as s:
        dept = await s.get(Department, org.dept_a["id"])
        dept.agent_quota = 5
        await s.commit()
    await _set_company_member_quota(org.member["id"], org.cid, 1)

    first = await _create_agent(client, org.member, org.cid, "成员一号",
                                dept=org.dept_a["id"], visibility="private")
    assert first.status_code == 201, first.text

    blocked = await _create_agent(client, org.member, org.cid, "成员二号",
                                  dept=org.dept_a["id"], visibility="private")
    assert blocked.status_code == 409, blocked.text

    await _set_company_member_quota(org.member["id"], org.cid, None)
    third = await _create_agent(client, org.member, org.cid, "成员三号",
                                dept=org.dept_a["id"], visibility="private")
    assert third.status_code == 201, third.text
