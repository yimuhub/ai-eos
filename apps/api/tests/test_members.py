"""成员管理接口（/companies/{cid}/members）行为护栏。

覆盖点：
* owner 建成员成功，返回体与列表都带部门/部门角色，且不含 password_hash；
* 重复加入 409、邮箱已存在复用账号且不重置密码、新账号缺密码 422；
* 普通成员不能建成员（404）；
* 部门负责人只能看到自己负责部门的成员，且同一人的部门列表被裁剪；
* 部门负责人能把自己部门的人设成 head，不能动别的部门（404）；
* 软停用后 status=suspended、账号仍能登录 / `/auth/me` 仍可查，
  但公司访问被真正切断（不是只改了个状态字段）；
* owner 不能被停用；
* company_admin 的授予/撤销只有 OWNER 能做；
* 事件日志 member.added / member.updated / member.suspended。

数据库一律走 conftest 的 tmp sqlite（`AsyncSessionLocal`），不碰 data/aieos.db。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.company import Department
from app.models.event import EventLog
from app.models.membership import CompanyMember, DepartmentMember
from app.models.user import User

MEMBER_PASSWORD = "Member1234pass"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------
def _member_payload(**overrides) -> dict:
    suffix = uuid.uuid4().hex[:8]
    body = {
        "email": f"member_{suffix}@example.com",
        "username": f"member_{suffix}",
        "password": MEMBER_PASSWORD,
        "role": "member",
    }
    body.update(overrides)
    return body


async def _make_dept(client, owner: dict, cid: str, name: str) -> dict:
    r = await client.post("/departments", params={"cid": cid},
                          json={"name": name}, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _add(client, actor: dict, cid: str, **overrides):
    """POST 一个成员，返回 (response, 请求体)。"""
    body = _member_payload(**overrides)
    r = await client.post(f"/companies/{cid}/members", json=body,
                          headers=actor["headers"])
    return r, body


async def _login(client, payload: dict) -> dict:
    r = await client.post("/auth/login", json={
        "identity": payload["username"], "password": payload["password"]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _list(client, actor: dict, cid: str):
    return await client.get(f"/companies/{cid}/members",
                            headers=actor["headers"])


async def _membership(cid: str, user_id: str) -> CompanyMember | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(CompanyMember).where(
                CompanyMember.company_id == cid,
                CompanyMember.user_id == user_id))
        ).scalars().first()


async def _dept_roles(cid: str, user_id: str) -> dict[str, str]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DepartmentMember.department_id,
                   DepartmentMember.role_in_dept).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id.in_(
                    select(Department.id).where(Department.company_id == cid)))
        )).all()
    return {r[0]: r[1] for r in rows}


async def _password_hash(user_id: str) -> str:
    async with AsyncSessionLocal() as db:
        return (await db.get(User, user_id)).password_hash


@pytest.fixture
async def org(client, make_user, make_company) -> SimpleNamespace:
    """一家公司 + 两个部门（owner 由 make_company 自动写入 owner 成员行）。"""
    owner = await make_user()
    company = await make_company(owner, name="成员管理公司")
    cid = company["id"]
    dept_a = await _make_dept(client, owner, cid, "A 部门")
    dept_b = await _make_dept(client, owner, cid, "B 部门")
    return SimpleNamespace(owner=owner, company=company, cid=cid,
                           dept_a=dept_a, dept_b=dept_b)


# ---------------------------------------------------------------------------
# 建成员
# ---------------------------------------------------------------------------
async def test_owner_adds_member_and_list_contains_departments(client, org):
    r, payload = await _add(client, org.owner, org.cid,
                            department_ids=[org.dept_a["id"]],
                            agent_quota=2)
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["email"] == payload["email"]
    assert body["role"] == "member"
    assert body["status"] == "active"
    assert body["agent_quota"] == 2
    assert body["user_id"]
    assert body["departments"] == [{
        "id": org.dept_a["id"],
        "department_id": org.dept_a["id"],
        "name": "A 部门",
        "role_in_dept": "member",
        "role": "member",
    }]
    assert "password_hash" not in body

    # 新账号能立即登录
    headers = await _login(client, payload)
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == body["user_id"]

    # owner 的全量列表：owner 自己 + 新成员，且部门信息完整
    listed = await _list(client, org.owner, org.cid)
    assert listed.status_code == 200, listed.text
    by_uid = {row["user_id"]: row for row in listed.json()}
    assert org.owner["id"] in by_uid, "owner 必须出现在成员列表里"
    assert body["user_id"] in by_uid
    assert by_uid[body["user_id"]]["departments"][0]["department_id"] == \
        org.dept_a["id"]


async def test_duplicate_join_is_409(client, org):
    first, payload = await _add(client, org.owner, org.cid)
    assert first.status_code == 201, first.text
    uid = first.json()["user_id"]

    again = await client.post(f"/companies/{org.cid}/members", json=payload,
                              headers=org.owner["headers"])
    assert again.status_code == 409, again.text

    rows = (await _list(client, org.owner, org.cid)).json()
    assert sum(1 for row in rows if row["user_id"] == uid) == 1


async def test_existing_email_reuses_account_without_resetting_password(
        client, org, make_user):
    existing = await make_user()
    old_hash = await _password_hash(existing["id"])

    r = await client.post(f"/companies/{org.cid}/members", json={
        "email": existing["email"], "role": "viewer"},
        headers=org.owner["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == existing["id"], "必须复用已有账号而不是新建"
    assert body["role"] == "viewer"
    assert body["username"] == existing["username"]
    assert await _password_hash(existing["id"]) == old_hash, "不能重置已有账号密码"

    # 旧密码仍然有效
    await _login(client, existing)


async def test_new_member_without_password_or_username_is_422(client, org):
    suffix = uuid.uuid4().hex[:8]
    no_password = await client.post(f"/companies/{org.cid}/members", json={
        "email": f"nopwd_{suffix}@example.com",
        "username": f"nopwd_{suffix}"}, headers=org.owner["headers"])
    assert no_password.status_code == 422, no_password.text

    no_username = await client.post(f"/companies/{org.cid}/members", json={
        "email": f"noname_{suffix}@example.com",
        "password": MEMBER_PASSWORD}, headers=org.owner["headers"])
    assert no_username.status_code == 422, no_username.text

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(User.id).where(
            User.email.in_([f"nopwd_{suffix}@example.com",
                            f"noname_{suffix}@example.com"])))).all()
    assert rows == [], "422 不能留下半套用户"


async def test_member_cannot_create_member(client, org):
    r, payload = await _add(client, org.owner, org.cid)
    assert r.status_code == 201
    member_headers = await _login(client, payload)

    denied = await client.post(
        f"/companies/{org.cid}/members",
        json=_member_payload(), headers=member_headers)
    assert denied.status_code == 404, denied.text


async def test_invalid_department_id_is_404_and_leaves_no_member(client, org):
    suffix = uuid.uuid4().hex[:8]
    r = await client.post(f"/companies/{org.cid}/members", json={
        "email": f"baddep_{suffix}@example.com",
        "username": f"baddep_{suffix}",
        "password": MEMBER_PASSWORD,
        "department_ids": [str(uuid.uuid4())],
    }, headers=org.owner["headers"])
    assert r.status_code == 404, r.text

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(
            User.email == f"baddep_{suffix}@example.com"))).scalars().first()
    assert user is None, "部门校验失败不能留下孤儿账号"


async def test_department_from_another_company_is_404(client, org,
                                                      make_user,
                                                      make_company):
    outsider = await make_user()
    other_co = await make_company(outsider, name="别家公司")
    foreign_dept = await _make_dept(client, outsider, other_co["id"], "外部部门")

    r, _ = await _add(client, org.owner, org.cid,
                      department_ids=[foreign_dept["id"]])
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 部门负责人：可见范围 / 设 head
# ---------------------------------------------------------------------------
async def test_dept_head_sees_only_own_department_and_trimmed_departments(
        client, org):
    head_r, head_p = await _add(client, org.owner, org.cid,
                                department_ids=[org.dept_a["id"]],
                                department_role="head")
    member_a_r, _ = await _add(client, org.owner, org.cid,
                               department_ids=[org.dept_a["id"]])
    member_ab_r, _ = await _add(
        client, org.owner, org.cid,
        department_ids=[org.dept_a["id"], org.dept_b["id"]])
    member_b_r, _ = await _add(client, org.owner, org.cid,
                               department_ids=[org.dept_b["id"]])
    for r in (head_r, member_a_r, member_ab_r, member_b_r):
        assert r.status_code == 201, r.text

    head_headers = await _login(client, head_p)
    listed = await _list(client, {"headers": head_headers}, org.cid)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    uids = {row["user_id"] for row in rows}

    assert head_r.json()["user_id"] in uids
    assert member_a_r.json()["user_id"] in uids
    assert member_ab_r.json()["user_id"] in uids
    assert member_b_r.json()["user_id"] not in uids, \
        "部门负责人不应看到别的部门的成员"

    ab_row = next(row for row in rows
                  if row["user_id"] == member_ab_r.json()["user_id"])
    assert {d["department_id"] for d in ab_row["departments"]} == \
        {org.dept_a["id"]}, "同一个人的部门列表也要裁剪到负责人自己的部门"


async def test_dept_head_promotes_member_in_own_department(client, org):
    head_r, head_p = await _add(client, org.owner, org.cid,
                                department_ids=[org.dept_a["id"]],
                                department_role="head")
    member_r, _ = await _add(client, org.owner, org.cid,
                             department_ids=[org.dept_a["id"]])
    assert head_r.status_code == 201 and member_r.status_code == 201

    head_headers = await _login(client, head_p)
    r = await client.patch(
        f"/companies/{org.cid}/members/{member_r.json()['id']}",
        json={"department_ids": [org.dept_a["id"]],
              "department_role": "head"},
        headers=head_headers)
    assert r.status_code == 200, r.text
    assert r.json()["departments"][0]["role_in_dept"] == "head"
    assert (await _dept_roles(org.cid, member_r.json()["user_id"]))[
        org.dept_a["id"]] == "head"


async def test_dept_head_cannot_promote_other_department_member(client, org):
    head_r, head_p = await _add(client, org.owner, org.cid,
                                department_ids=[org.dept_a["id"]],
                                department_role="head")
    member_b_r, _ = await _add(client, org.owner, org.cid,
                               department_ids=[org.dept_b["id"]])
    assert head_r.status_code == 201 and member_b_r.status_code == 201

    head_headers = await _login(client, head_p)
    # 目标本来就不在自己的部门 → 404
    r = await client.patch(
        f"/companies/{org.cid}/members/{member_b_r.json()['id']}",
        json={"department_ids": [org.dept_b["id"]],
              "department_role": "head"},
        headers=head_headers)
    assert r.status_code == 404, r.text

    # 即便只改部门角色、目标在 B 部门，也必须 404
    r2 = await client.patch(
        f"/companies/{org.cid}/members/{member_b_r.json()['id']}",
        json={"departments": [{"department_id": org.dept_b["id"],
                               "role_in_dept": "head"}]},
        headers=head_headers)
    assert r2.status_code == 404, r2.text

    assert (await _dept_roles(org.cid, member_b_r.json()["user_id"]))[
        org.dept_b["id"]] == "member", "越权的 head 设置不能落库"


async def test_dept_head_replacement_cannot_touch_other_departments(
        client, org):
    """负责人整体替换部门时，只能替换自己负责的部门；别人的 B 部门关系不能被顺手删掉。"""
    head_r, head_p = await _add(client, org.owner, org.cid,
                                department_ids=[org.dept_a["id"]],
                                department_role="head")
    member_r, _ = await _add(client, org.owner, org.cid,
                             department_ids=[org.dept_a["id"],
                                             org.dept_b["id"]])
    assert head_r.status_code == 201 and member_r.status_code == 201

    head_headers = await _login(client, head_p)
    r = await client.patch(
        f"/companies/{org.cid}/members/{member_r.json()['id']}",
        json={"department_ids": [org.dept_a["id"]]},
        headers=head_headers)
    assert r.status_code == 200, r.text
    roles = await _dept_roles(org.cid, member_r.json()["user_id"])
    assert roles == {org.dept_a["id"]: "member", org.dept_b["id"]: "member"}


async def test_second_head_requires_replace_head(client, org):
    first, _ = await _add(client, org.owner, org.cid,
                          department_ids=[org.dept_a["id"]],
                          department_role="head")
    assert first.status_code == 201
    conflict, _ = await _add(client, org.owner, org.cid,
                             department_ids=[org.dept_a["id"]],
                             department_role="head")
    assert conflict.status_code == 409, conflict.text

    replaced, payload = await _add(client, org.owner, org.cid,
                                   department_ids=[org.dept_a["id"]],
                                   department_role="head",
                                   replace_head=True)
    assert replaced.status_code == 201, replaced.text

    assert (await _dept_roles(org.cid, first.json()["user_id"]))[
        org.dept_a["id"]] == "member", "旧负责人应被降为普通成员"
    assert (await _dept_roles(org.cid, replaced.json()["user_id"]))[
        org.dept_a["id"]] == "head"


# ---------------------------------------------------------------------------
# 停用
# ---------------------------------------------------------------------------
async def test_suspended_member_loses_company_access_but_can_still_login(
        client, org):
    created, payload = await _add(client, org.owner, org.cid,
                                  department_ids=[org.dept_a["id"]])
    assert created.status_code == 201
    mid = created.json()["id"]
    uid = created.json()["user_id"]

    r = await client.delete(f"/companies/{org.cid}/members/{mid}",
                            headers=org.owner["headers"])
    assert r.status_code == 204, r.text

    row = await _membership(org.cid, uid)
    assert row is not None, "软停用不能物理删除成员行"
    assert row.status == "suspended"
    assert await _dept_roles(org.cid, uid) == {}, \
        "停用必须清掉部门成员行，否则 resolve_role 仍会把他当 MEMBER"

    # 账号本身没被禁用：登录与 /auth/me 仍可用（按现有机制）
    headers = await _login(client, payload)
    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text

    # 但公司访问已被切断
    company = await client.get(f"/companies/{org.cid}", headers=headers)
    assert company.status_code == 404, "停用后不应还能进入公司"
    listed = await _list(client, {"headers": headers}, org.cid)
    assert listed.status_code == 404

    # 重复停用幂等
    again = await client.delete(f"/companies/{org.cid}/members/{mid}",
                                headers=org.owner["headers"])
    assert again.status_code == 204


async def test_dept_head_cannot_suspend_or_set_quota(client, org):
    """停用与配额是 MANAGE 级处置权，部门负责人不能通过 PATCH 绕过去。

    回归背景：PATCH 原先只校验部门归属，负责人可以停用本部门成员；
    停用后目标不再属于其部门作用域，负责人自己也无法恢复，等于
    一个不可逆的 MANAGE 操作被下发给了 MEMBER 级角色。
    """
    head_r, head_p = await _add(client, org.owner, org.cid,
                                department_ids=[org.dept_a["id"]],
                                department_role="head")
    member_r, _ = await _add(client, org.owner, org.cid,
                             department_ids=[org.dept_a["id"]])
    assert head_r.status_code == 201 and member_r.status_code == 201

    head_headers = await _login(client, head_p)
    mid = member_r.json()["id"]
    uid = member_r.json()["user_id"]

    suspended = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"status": "suspended"}, headers=head_headers)
    assert suspended.status_code == 404, suspended.text

    quota = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"agent_quota": 0}, headers=head_headers)
    assert quota.status_code == 404, quota.text

    row = await _membership(org.cid, uid)
    assert row.status == "active", "越权的停用不能落库"
    assert row.agent_quota is None, "越权的配额修改不能落库"

    # 负责人仍可管部门归属，不被这次收紧误伤
    moved = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"department_ids": [org.dept_a["id"]],
              "department_role": "head"},
        headers=head_headers)
    assert moved.status_code == 200, moved.text

    # owner 仍然可以停用
    by_owner = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"status": "suspended"}, headers=org.owner["headers"])
    assert by_owner.status_code == 200, by_owner.text
    assert (await _membership(org.cid, uid)).status == "suspended"


async def test_owner_cannot_be_suspended(client, org):
    admin_r, admin_p = await _add(client, org.owner, org.cid,
                                  role="company_admin")
    assert admin_r.status_code == 201
    admin_headers = await _login(client, admin_p)

    rows = (await _list(client, org.owner, org.cid)).json()
    owner_mid = next(row["id"] for row in rows
                     if row["user_id"] == org.owner["id"])

    by_admin = await client.delete(f"/companies/{org.cid}/members/{owner_mid}",
                                   headers=admin_headers)
    assert by_admin.status_code == 409, by_admin.text

    by_owner = await client.delete(f"/companies/{org.cid}/members/{owner_mid}",
                                   headers=org.owner["headers"])
    assert by_owner.status_code == 409, "owner 也不能停用自己"

    assert (await _membership(org.cid, org.owner["id"])).status == "active"


# ---------------------------------------------------------------------------
# company_admin 的授予/撤销
# ---------------------------------------------------------------------------
async def test_only_owner_can_grant_or_revoke_company_admin(client, org):
    admin_r, admin_p = await _add(client, org.owner, org.cid,
                                  role="company_admin")
    member_r, _ = await _add(client, org.owner, org.cid)
    assert admin_r.status_code == 201 and member_r.status_code == 201
    admin_headers = await _login(client, admin_p)

    denied_create = await client.post(
        f"/companies/{org.cid}/members",
        json=_member_payload(role="company_admin"), headers=admin_headers)
    assert denied_create.status_code == 404, denied_create.text

    mid = member_r.json()["id"]
    denied_patch = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"role": "company_admin"}, headers=admin_headers)
    assert denied_patch.status_code == 404, denied_patch.text

    granted = await client.patch(f"/companies/{org.cid}/members/{mid}",
                                 json={"role": "company_admin"},
                                 headers=org.owner["headers"])
    assert granted.status_code == 200, granted.text
    assert granted.json()["role"] == "company_admin"

    denied_revoke = await client.patch(
        f"/companies/{org.cid}/members/{mid}",
        json={"role": "member"}, headers=admin_headers)
    assert denied_revoke.status_code == 404, denied_revoke.text

    revoked = await client.patch(f"/companies/{org.cid}/members/{mid}",
                                 json={"role": "member"},
                                 headers=org.owner["headers"])
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["role"] == "member"


# ---------------------------------------------------------------------------
# 越权与事件日志
# ---------------------------------------------------------------------------
async def test_unrelated_user_gets_404_on_member_endpoints(client, org,
                                                           make_user):
    stranger = await make_user()
    listed = await _list(client, stranger, org.cid)
    assert listed.status_code == 404

    created = await client.post(f"/companies/{org.cid}/members",
                                json=_member_payload(),
                                headers=stranger["headers"])
    assert created.status_code == 404


async def test_member_events_are_logged(client, org):
    created, _ = await _add(client, org.owner, org.cid)
    assert created.status_code == 201
    mid = created.json()["id"]
    uid = created.json()["user_id"]

    patched = await client.patch(f"/companies/{org.cid}/members/{mid}",
                                 json={"agent_quota": 5},
                                 headers=org.owner["headers"])
    assert patched.status_code == 200, patched.text

    suspended = await client.delete(f"/companies/{org.cid}/members/{mid}",
                                    headers=org.owner["headers"])
    assert suspended.status_code == 204

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(EventLog.event_type, EventLog.payload).where(
                EventLog.company_id == org.cid))).all()
    types = [r[0] for r in rows]
    assert "member.added" in types
    assert "member.updated" in types
    assert "member.suspended" in types

    added = next(r[1] for r in rows if r[0] == "member.added")
    assert added["user_id"] == uid
    updated = next(r[1] for r in rows if r[0] == "member.updated")
    assert updated["agent_quota"] == 5