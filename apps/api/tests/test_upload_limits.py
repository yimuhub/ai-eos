"""上传限额、文件名与扩展名白名单（审查 S9 回归护栏）。

覆盖三条真实攻击面，全部走 HTTP，不依赖真实数据库：
1. 超限 → 413，且半成品被删除、不入库；
2. 路径穿越文件名（../../etc/passwd.md）不会越界：落盘名由服务端生成；
3. 白名单外扩展名 → 400，且不落盘。

存储根目录一律用 pytest 的 tmp_path（通过 settings.storage_dir /
AIEOS_KB_UPLOAD_DIR 重定向），绝不触碰 apps/api/data/aieos.db。
"""
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.uploads import (ASSET_ALLOWED_EXTENSIONS, KB_ALLOWED_EXTENSIONS,
                              safe_original_name)

MB = 1024 * 1024


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """把两处上传根目录都重定向到 tmp_path。"""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setenv("AIEOS_KB_UPLOAD_DIR", str(tmp_path / "kb"))
    return tmp_path


@pytest.fixture
def one_mb_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)


async def _make_agent(client, user, company_id: str) -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": "上传测试员工",
        "role": "research", "department": "研究部"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _upload_asset(client, user, company_id, filename, data,
                        mime="application/octet-stream"):
    return await client.post(
        f"/assets/upload?company_id={company_id}",
        files={"file": (filename, data, mime)}, headers=user["headers"])


async def _upload_kb(client, user, agent_id, filename, data,
                     mime="application/octet-stream"):
    return await client.post(
        f"/kb2/agents/{agent_id}/upload",
        files={"file": (filename, data, mime)}, headers=user["headers"])


# ---------------- 单元级：文件名清洗 ----------------

def test_safe_original_name_strips_paths_and_controls():
    assert safe_original_name("../../etc/passwd.md") == "passwd.md"
    assert safe_original_name("..\\..\\windows\\evil.txt") == "evil.txt"
    assert safe_original_name("a\x00b\n.md") == "ab.md"
    assert safe_original_name("../") == "upload"
    assert safe_original_name(None) == "upload"


def test_whitelists_are_separate_and_reject_executables():
    # 文档、表格/演示与图片：两条路径都允许（与前端 accept 列表一致）
    for ext in (".md", ".txt", ".pdf", ".docx", ".csv", ".json",
                ".xlsx", ".pptx", ".png", ".jpg"):
        assert ext in KB_ALLOWED_EXTENSIONS
        assert ext in ASSET_ALLOWED_EXTENSIONS
    # 媒体与代码/脚本只属于资产中心，不该进入知识库解析路径
    for ext in (".mp4", ".mp3", ".py", ".js"):
        assert ext in ASSET_ALLOWED_EXTENSIONS
        assert ext not in KB_ALLOWED_EXTENSIONS
    for ext in (".exe", ".sh", ".bat", ".html", ".svg"):
        assert ext not in KB_ALLOWED_EXTENSIONS
        assert ext not in ASSET_ALLOWED_EXTENSIONS


# ---------------- assets 上传 ----------------

async def test_asset_upload_over_limit_413_and_no_leftover(
        client, tenants, storage, one_mb_limit):
    alice = tenants.alice
    r = await _upload_asset(client, alice, tenants.alice_co["id"],
                            "big.md", b"x" * (MB + 1), "text/markdown")
    assert r.status_code == 413, r.text

    uploads = storage / "storage" / "uploads"
    assert [p for p in uploads.glob("*") if p.is_file()] == []
    r = await client.get(f"/assets?company_id={tenants.alice_co['id']}",
                         headers=alice["headers"])
    assert r.status_code == 200 and r.json() == []


async def test_asset_upload_traversal_filename_stays_inside_root(
        client, tenants, storage):
    alice = tenants.alice
    r = await _upload_asset(client, alice, tenants.alice_co["id"],
                            "../../../../etc/aieos_passwd.md",
                            b"# hi", "text/markdown")
    assert r.status_code == 201, r.text
    body = r.json()
    # 原始文件名只作元数据（保留 basename），不参与落盘
    assert body["name"] == "aieos_passwd.md"

    uploads = storage / "storage" / "uploads"
    files = [p for p in uploads.glob("*") if p.is_file()]
    assert len(files) == 1
    stored = files[0]
    assert "aieos_passwd" not in stored.name
    assert stored.suffix == ".md"
    # DB 里记录的是相对路径，且解析后仍在存储根内
    listed = (await client.get(f"/assets?company_id={tenants.alice_co['id']}",
                               headers=alice["headers"])).json()
    rel = listed[0]["path"]
    assert not Path(rel).is_absolute()
    resolved = (storage / "storage" / rel).resolve()
    assert (storage / "storage").resolve() in resolved.parents
    # 越界目标位置没有被写入
    assert not (storage / "etc" / "aieos_passwd.md").exists()


async def test_asset_upload_rejects_non_whitelisted_extension(
        client, tenants, storage):
    alice = tenants.alice
    r = await _upload_asset(client, alice, tenants.alice_co["id"],
                            "payload.exe", b"MZ", "application/octet-stream")
    assert r.status_code == 400, r.text
    uploads = storage / "storage" / "uploads"
    assert [p for p in uploads.glob("*") if p.is_file()] == []


# ---------------- 知识库上传 ----------------

async def test_kb_upload_over_limit_413_and_no_leftover(
        client, tenants, storage, one_mb_limit):
    alice = tenants.alice
    agent_id = await _make_agent(client, alice, tenants.alice_co["id"])
    r = await _upload_kb(client, alice, agent_id, "big.txt", b"y" * (MB + 1),
                         "text/plain")
    assert r.status_code == 413, r.text

    agent_dir = storage / "kb" / agent_id
    assert [p for p in agent_dir.rglob("*") if p.is_file()] == []


async def test_kb_upload_traversal_filename_stays_inside_root(
        client, tenants, storage):
    alice = tenants.alice
    agent_id = await _make_agent(client, alice, tenants.alice_co["id"])
    r = await _upload_kb(client, alice, agent_id,
                         "../../../etc/aieos_secret.txt", b"hello", "text/plain")
    assert r.status_code == 200, r.text
    assert r.json()["converted"] is True

    agent_dir = storage / "kb" / agent_id
    files = [p for p in agent_dir.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert "aieos_secret" not in files[0].name
    assert files[0].suffix == ".txt"
    assert not (storage / "etc" / "aieos_secret.txt").exists()

    docs = (await client.get(f"/kb2/agents/{agent_id}/documents",
                             headers=alice["headers"])).json()
    assert docs[0]["original_name"] == "aieos_secret.txt"


async def test_kb_upload_rejects_non_whitelisted_extension(
        client, tenants, storage):
    alice = tenants.alice
    agent_id = await _make_agent(client, alice, tenants.alice_co["id"])
    r = await _upload_kb(client, alice, agent_id, "page.html",
                         b"<script>alert(1)</script>", "text/html")
    assert r.status_code == 400, r.text
    agent_dir = storage / "kb" / agent_id
    assert not agent_dir.exists() or [
        p for p in agent_dir.rglob("*") if p.is_file()] == []
