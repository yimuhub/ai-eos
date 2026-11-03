"""插件库与本机操作策略：目录、检测/安装登记、健康检查、三档审批与硬拦截。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.membership import CompanyMember
from app.models.plugin import ComputerPolicy, PluginInstallation


def _detected(path: str = "/usr/local/bin/agent-desktop") -> dict:
    return {"found": True, "binary": "agent-desktop", "path": path,
            "version": "1.0.2", "platform": "darwin-arm64", "error": None}


@pytest.mark.asyncio
async def test_catalog_lists_agent_desktop_and_install_state(client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]

    r = await client.get("/plugins/catalog", params={"company_id": company_id},
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {p["id"]: p for p in body["plugins"]}
    assert by_id["agent-desktop"]["installable"] is True
    assert by_id["agent-desktop"]["installed"] is False
    assert by_id["git-workspace"]["installable"] is False
    assert body["installed_count"] == 0
    assert body["installable_count"] == 1

    bob = await client.get("/plugins/catalog", params={"company_id": company_id},
                           headers=tenants.bob["headers"])
    assert bob.status_code == 404


@pytest.mark.asyncio
async def test_detect_registers_cli_install_and_health(client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    from app.services import plugin_service

    monkeypatch.setattr(plugin_service, "detect_plugin",
                        lambda plugin_id: _detected())

    r = await client.post("/plugins/agent-desktop/detect",
                          params={"company_id": company_id},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["installation"]["source"] == "cli"
    assert body["installation"]["version"] == "1.0.2"
    assert body["installation"]["executable"] == "/usr/local/bin/agent-desktop"

    catalog = (await client.get("/plugins/catalog",
                                params={"company_id": company_id},
                                headers=alice["headers"])).json()
    assert catalog["installed_count"] == 1

    health = await client.post("/plugins/agent-desktop/health",
                               params={"company_id": company_id},
                               headers=alice["headers"])
    assert health.status_code == 200, health.text
    assert health.json()["installation"]["last_health_ok"] is True


@pytest.mark.asyncio
async def test_managed_install_uses_fixed_package_then_uninstalls(
        client, tenants, monkeypatch):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    from app.services import plugin_service

    calls: list[list[str]] = []

    def fake_run(argv, timeout=15):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin_service.shutil, "which", lambda name: "/opt/npm")
    monkeypatch.setattr(plugin_service, "_run", fake_run)
    monkeypatch.setattr(plugin_service, "detect_plugin",
                        lambda plugin_id: _detected())

    r = await client.post("/plugins/agent-desktop/install",
                          params={"company_id": company_id},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["installation"]["source"] == "managed"
    assert calls[0] == ["/opt/npm", "install", "-g", "agent-desktop"]

    removed = await client.delete(
        "/plugins/agent-desktop",
        params={"company_id": company_id, "remove_package": True},
        headers=alice["headers"])
    assert removed.status_code == 200, removed.text
    assert calls[1] == ["/opt/npm", "uninstall", "-g", "agent-desktop"]

    async with AsyncSessionLocal() as db:
        left = (await db.execute(select(PluginInstallation).where(
            PluginInstallation.company_id == company_id))).scalars().all()
        assert left == []


@pytest.mark.asyncio
async def test_install_requires_manage_capability(client, tenants):
    from app.models.user import User

    company_id = tenants.alice_co["id"]
    async with AsyncSessionLocal() as db:
        user = User(username="plugin-viewer", email="plugin-viewer@test.io",
                    password_hash="x")
        db.add(user)
        await db.flush()
        db.add(CompanyMember(company_id=company_id, user_id=user.id,
                             role="viewer", status="active"))
        await db.commit()
        viewer_id = user.id

    from app.core.security import create_access_token
    token = create_access_token(viewer_id)
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/plugins/agent-desktop/install",
                          params={"company_id": company_id}, headers=headers)
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_policy_default_blocks_hard_actions_and_requires_approval(
        client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]

    unconfigured = await client.get("/computer-agent/policy",
                                    params={"company_id": company_id},
                                    headers=alice["headers"])
    assert unconfigured.status_code == 200
    assert unconfigured.json()["configured"] is False
    assert unconfigured.json()["mode"] == "default"

    legacy = await client.post("/computer-agent/evaluate",
                               json={"action_type": "run_shell",
                                     "params": {"command": "ls"}},
                               params={"company_id": company_id},
                               headers=alice["headers"])
    assert legacy.json()["decision"] == "legacy"

    saved = await client.put("/computer-agent/policy",
                             json={"mode": "default"},
                             params={"company_id": company_id},
                             headers=alice["headers"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True

    async def verdict(action: str, params: dict) -> dict:
        r = await client.post("/computer-agent/evaluate",
                              json={"action_type": action, "params": params},
                              params={"company_id": company_id},
                              headers=alice["headers"])
        assert r.status_code == 200, r.text
        return r.json()

    assert (await verdict("screenshot", {}))["decision"] == "allow"
    assert (await verdict("run_shell", {"command": "ls -la"}))["decision"] == "approval"
    blocked = await verdict("run_shell", {"command": "rm -rf /"})
    assert blocked["decision"] == "blocked"
    assert "硬拦截" in blocked["reason"]
    secrets = await verdict("run_write", {"path": "~/.ssh/id_rsa"})
    assert secrets["decision"] == "blocked"
    terminal = await verdict("type_text", {"app": "iTerm2", "text": "rm -rf /"})
    assert terminal["decision"] == "blocked"

    auto = await client.put("/computer-agent/policy",
                            json={"mode": "auto",
                                  "auto_dirs": ["~/Documents/ai-eos"],
                                  "auto_commands": ["git status"],
                                  "auto_apps": ["Finder"]},
                            params={"company_id": company_id},
                            headers=alice["headers"])
    assert auto.status_code == 200, auto.text

    assert (await verdict("run_write", {
        "path": "~/Documents/ai-eos/README.md"}))["decision"] == "allow"
    assert (await verdict("run_write", {
        "path": "~/Documents/other/x.txt"}))["decision"] == "approval"
    assert (await verdict("run_shell", {
        "command": "git status"}))["decision"] == "allow"
    # 带 shell 元字符的命令不允许靠前缀白名单绕过
    assert (await verdict("run_shell", {
        "command": "git status; rm -rf build"}))["decision"] == "approval"
    assert (await verdict("run_app", {"app": "Finder"}))["decision"] == "allow"


@pytest.mark.asyncio
async def test_full_control_is_time_limited_and_expires(client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]
    await client.put("/computer-agent/policy", json={"mode": "default"},
                     params={"company_id": company_id},
                     headers=alice["headers"])

    started = await client.post("/computer-agent/full-control",
                                params={"company_id": company_id,
                                        "minutes": 30},
                                headers=alice["headers"])
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == "full"
    assert started.json()["full_control_until"]

    r = await client.post("/computer-agent/evaluate",
                          json={"action_type": "run_shell",
                                "params": {"command": "rm -rf build"}},
                          params={"company_id": company_id},
                          headers=alice["headers"])
    assert r.json()["decision"] == "allow"

    # 到期后自动降回默认审批，绝不静默续期
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ComputerPolicy).where(
            ComputerPolicy.company_id == company_id))).scalars().one()
        row.full_control_until = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1))
        await db.commit()

    expired = await client.post("/computer-agent/evaluate",
                                json={"action_type": "run_shell",
                                      "params": {"command": "rm -rf build"}},
                                params={"company_id": company_id},
                                headers=alice["headers"])
    assert expired.json()["decision"] == "approval"
    assert expired.json()["mode"] == "default"

    exited = await client.post("/computer-agent/exit-full-control",
                               params={"company_id": company_id},
                               headers=alice["headers"])
    assert exited.status_code == 200
    assert exited.json()["mode"] == "default"


@pytest.mark.asyncio
async def test_execute_hard_block_is_audited_and_cross_tenant_hidden(
        client, tenants):
    alice = tenants.alice
    company_id = tenants.alice_co["id"]

    r = await client.post("/computer-agent/execute",
                          json={"action_type": "run_shell",
                                "params": {"command": "rm -rf /"}},
                          params={"company_id": company_id},
                          headers=alice["headers"])
    assert r.status_code == 403, r.text
    assert "硬拦截" in r.text

    logs = await client.get("/computer-agent/logs",
                            params={"company_id": company_id},
                            headers=alice["headers"])
    assert logs.status_code == 200
    denied = [row for row in logs.json() if row["allowed"] is False]
    assert denied and "硬拦截" in (denied[0]["deny_reason"] or "")

    hidden = await client.get("/computer-agent/policy",
                              params={"company_id": company_id},
                              headers=tenants.bob["headers"])
    assert hidden.status_code == 404
