"""E8 BYOK 密钥库：只存密文、列表不含密文、轮换/删除/校验与租户隔离。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.company_secret import CompanySecret
from app.services.secret_vault import decrypt, encrypt, vault_available

pytestmark = pytest.mark.asyncio

NAME = "openai.key-1"


async def _put(client, user, company_id: str, name: str, value: str):
    return await client.put(f"/byok/{name}",
                            json={"company_id": company_id, "value": value},
                            headers=user["headers"])


async def _stored(company_id: str, name: str) -> CompanySecret | None:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(CompanySecret)
            .where(CompanySecret.company_id == company_id,
                   CompanySecret.name == name))).scalars().first()


async def test_put_stores_ciphertext_and_decrypts_back(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    plaintext = "sk-test-very-secret-123456"

    r = await _put(client, alice, co["id"], NAME, plaintext)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == NAME
    assert body["rotated"] is False
    assert body["updated_at"] is not None
    assert plaintext not in r.text

    row = await _stored(co["id"], NAME)
    assert row is not None
    assert row.ciphertext != plaintext, "数据库里绝不能出现明文"
    assert decrypt(row.ciphertext) == plaintext

    # 列表只有元数据，绝不返回密文/明文
    r = await client.get(f"/byok?company_id={co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert set(items[0]) == {"name", "created_at", "updated_at"}
    assert plaintext not in r.text
    assert row.ciphertext not in r.text


async def test_rotation_rewrites_ciphertext(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    old_value = "sk-old-value"
    new_value = "sk-new-value"

    r = await _put(client, alice, co["id"], NAME, old_value)
    assert r.status_code == 200, r.text
    first = await _stored(co["id"], NAME)
    assert first is not None
    old_ciphertext = first.ciphertext

    r = await _put(client, alice, co["id"], NAME, new_value)
    assert r.status_code == 200, r.text
    assert r.json()["rotated"] is True

    second = await _stored(co["id"], NAME)
    assert second is not None
    assert second.id == first.id, "轮换是更新同一行，不是新建"
    assert second.ciphertext != old_ciphertext
    assert decrypt(second.ciphertext) == new_value
    assert old_value not in second.ciphertext


async def test_delete_and_missing_name(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    r = await _put(client, alice, co["id"], NAME, "sk-delete-me")
    assert r.status_code == 200, r.text

    r = await client.delete(f"/byok/{NAME}?company_id={co['id']}",
                            headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": NAME}
    assert await _stored(co["id"], NAME) is None

    r = await client.delete(f"/byok/{NAME}?company_id={co['id']}",
                            headers=alice["headers"])
    assert r.status_code == 404


async def test_invalid_name_and_value_are_400(client, tenants):
    alice, co = tenants.alice, tenants.alice_co

    r = await _put(client, alice, co["id"], "bad$name", "sk-x")
    assert r.status_code == 400
    r = await _put(client, alice, co["id"], "a" * 81, "sk-x")
    assert r.status_code == 400
    r = await _put(client, alice, co["id"], NAME, "")
    assert r.status_code == 400
    r = await _put(client, alice, co["id"], NAME, "x" * 4097)
    assert r.status_code == 400

    # 合法边界：80 字符名称 + 4096 字符值
    r = await _put(client, alice, co["id"], "a" * 80, "x" * 4096)
    assert r.status_code == 200, r.text


async def test_verify_reports_configured_only(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    await _put(client, alice, co["id"], NAME, "sk-verify-me")

    r = await client.post(f"/byok/{NAME}/verify",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert "sk-verify-me" not in r.text

    r = await client.post("/byok/missing/verify",
                          json={"company_id": co["id"]},
                          headers=alice["headers"])
    assert r.status_code == 404


async def test_cross_tenant_is_404(client, tenants):
    alice, alice_co = tenants.alice, tenants.alice_co
    bob = tenants.bob
    await _put(client, alice, alice_co["id"], NAME, "sk-alice-only")

    r = await _put(client, bob, alice_co["id"], "bob-key", "sk-bob")
    assert r.status_code == 404

    r = await client.get(f"/byok?company_id={alice_co['id']}",
                         headers=bob["headers"])
    assert r.status_code == 404

    r = await client.delete(f"/byok/{NAME}?company_id={alice_co['id']}",
                            headers=bob["headers"])
    assert r.status_code == 404

    r = await client.post(f"/byok/{NAME}/verify",
                          json={"company_id": alice_co["id"]},
                          headers=bob["headers"])
    assert r.status_code == 404

    # 外租户的写入没有留下任何行
    assert await _stored(alice_co["id"], "bob-key") is None


async def test_vault_roundtrip_and_availability():
    assert vault_available() is True
    ciphertext = encrypt("sk-本地-测试")
    assert ciphertext != "sk-本地-测试"
    assert decrypt(ciphertext) == "sk-本地-测试"
