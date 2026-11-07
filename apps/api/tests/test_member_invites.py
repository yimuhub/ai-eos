"""成员邀请链接行为护栏。

覆盖点：
* 生成 → 公开预览 → 接受 → 成员/部门关系落库，链接用过即失效；
* 库里只存 SHA-256，列表接口不回显明文 token；
* 撤销 / 过期 / 已用 三种不可用状态都给出明确原因；
* 跨租户一律 404；普通成员不能发邀请；company_admin 邀请只有 OWNER 能发；
* 接受邀请必须登录。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.company import Department
from app.models.member_invite import MemberInvite, utcnow
from app.models.membership import CompanyMember, DepartmentMember


async def _make_dept(client, owner: dict, cid: str, name: str) -> dict:
    r = await client.post("/departments", params={"cid": cid},
                          json={"name": name}, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _invite(client, actor: dict, cid: str, **body) -> dict:
    r = await client.post(f"/companies/{cid}/invites", json=body,
                          headers=actor["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _membership(cid: str, user_id: str) -> CompanyMember | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(CompanyMember).where(CompanyMember.company_id == cid,
                                        CompanyMember.user_id == user_id))
        ).scalars().first()


async def _dept_ids(cid: str, user_id: str) -> list[str]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(DepartmentMember.department_id).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id.in_(
                    select(Department.id).where(Department.company_id == cid)))
        )).scalars().all())


async def test_invite_create_preview_accept_happy_path(client, make_user,
                                                       make_company):
    owner = await make_user()
    company = await make_company(owner, name="邀请测试公司")
    cid = company["id"]
    dept = await _make_dept(client, owner, cid, "市场部")

    created = await _invite(client, owner, cid, role="member",
                            department_id=dept["id"])
    token = created["token"]
    assert created["link_path"] == f"/join/{token}"
    assert created["role"] == "member"
    assert created["department_name"] == "市场部"
    assert created["state"] == "valid"

    # 库里只有哈希，没有明文
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(MemberInvite))).scalars().one()
        assert row.token_hash == MemberInvite.hash_token(token)
        assert token not in row.token_hash

    # 未登录也能预览，但拿不到公司 id / 成员信息
    preview = await client.get(f"/invites/{token}")
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert p["valid"] is True
    assert p["company_name"] == "邀请测试公司"
    assert p["department_name"] == "市场部"
    assert "company_id" not in p

    invitee = await make_user()
    accepted = await client.post(f"/invites/{token}/accept",
                                 headers=invitee["headers"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["company_id"] == cid

    member = await _membership(cid, invitee["id"])
    assert member is not None and member.status == "active"
    assert member.role == "member"
    assert await _dept_ids(cid, invitee["id"]) == [dept["id"]]

    # 链接一次性：再用 → 410；预览也会明确说已使用
    again = await client.post(f"/invites/{token}/accept",
                              headers=invitee["headers"])
    assert again.status_code == 410
    p2 = (await client.get(f"/invites/{token}")).json()
    assert p2["valid"] is False and p2["state"] == "used"

    # 列表接口不回显明文
    listed = await client.get(f"/companies/{cid}/invites",
                              headers=owner["headers"])
    assert listed.status_code == 200
    items = listed.json()["invites"]
    assert len(items) == 1
    assert items[0]["link_path"] is None
    assert token not in listed.text


async def test_invite_revoke_and_expiry(client, make_user, make_company):
    owner = await make_user()
    cid = (await make_company(owner))["id"]

    revoked = await _invite(client, owner, cid)
    r = await client.delete(f"/companies/{cid}/invites/{revoked['id']}",
                            headers=owner["headers"])
    assert r.status_code == 200 and r.json()["state"] == "revoked"
    assert (await client.get(f"/invites/{revoked['token']}")).json()["state"] \
        == "revoked"
    invitee = await make_user()
    assert (await client.post(f"/invites/{revoked['token']}/accept",
                              headers=invitee["headers"])).status_code == 410

    expired = await _invite(client, owner, cid)
    async with AsyncSessionLocal() as db:
        row = await db.get(MemberInvite, expired["id"])
        row.expires_at = utcnow() - timedelta(minutes=1)
        await db.commit()
    assert (await client.get(f"/invites/{expired['token']}")).json()["state"] \
        == "expired"
    assert (await client.post(f"/invites/{expired['token']}/accept",
                              headers=invitee["headers"])).status_code == 410


async def test_invite_permissions_and_tenant_isolation(client, tenants):
    alice, bob = tenants.alice, tenants.bob
    cid = tenants.alice_co["id"]

    # 不是本公司的人：看不到、发不了、撤销不了（一律 404）
    assert (await client.get(f"/companies/{cid}/invites",
                             headers=bob["headers"])).status_code == 404
    assert (await client.post(f"/companies/{cid}/invites", json={},
                              headers=bob["headers"])).status_code == 404

    created = await _invite(client, alice, cid)
    assert (await client.delete(
        f"/companies/{cid}/invites/{created['id']}",
        headers=bob["headers"])).status_code == 404

    # 普通成员不能发邀请
    member = await make_member(client, alice, cid, "member")
    r = await client.post(f"/companies/{cid}/invites", json={},
                          headers=member["headers"])
    assert r.status_code == 404
    assert (await client.get(f"/companies/{cid}/invites",
                             headers=member["headers"])).status_code == 404

    # company_admin 能发普通邀请，但发 company_admin 邀请只有 OWNER 行
    admin = await make_member(client, alice, cid, "company_admin")
    r = await client.post(f"/companies/{cid}/invites", json={"role": "member"},
                          headers=admin["headers"])
    assert r.status_code == 201
    r = await client.post(f"/companies/{cid}/invites",
                          json={"role": "company_admin"},
                          headers=admin["headers"])
    assert r.status_code == 404


async def test_invite_rejects_owner_role_and_foreign_department(
        client, make_user, make_company):
    owner = await make_user()
    cid = (await make_company(owner))["id"]
    other_owner = await make_user()
    other_cid = (await make_company(other_owner))["id"]
    foreign_dept = await _make_dept(client, other_owner, other_cid, "别家部门")

    r = await client.post(f"/companies/{cid}/invites", json={"role": "owner"},
                          headers=owner["headers"])
    assert r.status_code == 422
    r = await client.post(f"/companies/{cid}/invites",
                          json={"department_id": foreign_dept["id"]},
                          headers=owner["headers"])
    assert r.status_code == 404


async def test_accept_requires_login_and_rejects_existing_member(
        client, make_user, make_company):
    owner = await make_user()
    cid = (await make_company(owner))["id"]
    created = await _invite(client, owner, cid)

    r = await client.post(f"/invites/{created['token']}/accept")
    assert r.status_code == 401

    # owner 自己点链接：已是成员，明确 409 而不是重复插入
    r = await client.post(f"/invites/{created['token']}/accept",
                          headers=owner["headers"])
    assert r.status_code == 409


async def make_member(client, owner: dict, cid: str, role: str) -> dict:
    """建一个可直接登录的成员（走 /members 接口，密码由这里掌握）。"""
    suffix = uuid.uuid4().hex[:8]
    body = {
        "email": f"m_{suffix}@example.com",
        "username": f"m_{suffix}",
        "password": "Member1234pass",
        "role": role,
    }
    r = await client.post(f"/companies/{cid}/members", json=body,
                          headers=owner["headers"])
    assert r.status_code == 201, r.text
    r = await client.post("/auth/login", json={
        "identity": body["username"], "password": body["password"]})
    assert r.status_code == 200, r.text
    return {"id": r.json()["user"]["id"],
            "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}}
