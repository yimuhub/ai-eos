"""文件级本机操作（read_file / write_file / apply_patch）的回归测试。

## 测试纪律

所有用例只在 `tmp_path` 里读写，备份目录也被改到 tmp——
既不会碰仓库里的真实文件，更不会碰 `apps/api/.env`（它本来就被红线挡着）。

覆盖四件事：

1. 路径守卫：全盘可达，但凭据目录、`.env`、`/etc` 一律拒绝；
2. 读：分页、字节上限、二进制拒绝，且拒绝时说明原因而不是返回空；
3. 写与补丁：先备份再落盘、diff 正确、锚点缺失/歧义整体放弃、
   dry_run 零副作用、换行符不被静默归一化；
4. 与三档策略/审批门的接线：读算只读、写算高危、白名单目录自动放行。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import app.computer_files as cf
from app.computer_agent import ComputerPermission
from app.db.session import AsyncSessionLocal
from app.models.plugin import ComputerPolicy
from app.services import computer_policy as policy
from app.services import computer_service


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """一个干净的临时"磁盘"：文件写在这里，备份也落在这里。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(cf, "_backup_root", lambda: tmp_path / "backups")
    # 相对路径基准也指过来，免得测试去解析真实仓库
    monkeypatch.setattr(cf, "PROJECT_ROOT", work)
    return work


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ============================================================ 路径守卫


@pytest.mark.parametrize("bad", [
    "~/.ssh/id_rsa",
    "~/.aws/credentials",
    "/etc/passwd",
    "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
])
def test_hard_blocked_paths_are_refused(bad):
    with pytest.raises(cf.FileOpError) as exc:
        cf._resolve_path(bad, must_exist=False)
    assert "硬拦截" in str(exc.value)


def test_env_file_is_refused_even_inside_the_workspace(workspace):
    """沙箱根之内也要挡：仓库里的 .env 是密钥的实际所在地。"""
    target = _write(workspace / "apps" / "api" / ".env", "JWT_SECRET=abc\n")
    with pytest.raises(cf.FileOpError) as exc:
        cf._resolve_path(str(target), must_exist=True)
    assert "敏感" in str(exc.value) or ".env" in str(exc.value)


def test_relative_paths_resolve_against_the_workspace_root(workspace):
    _write(workspace / "src" / "main.py", "print(1)\n")
    resolved = cf._resolve_path("src/main.py", must_exist=True)
    assert resolved == (workspace / "src" / "main.py").resolve()


def test_dotdot_cannot_smuggle_a_blocked_path_past_the_guard(workspace):
    """`..` 在判断之前就被消解——用相对写法绕进 /etc 一样挡下。"""
    sneaky = "/".join([".."] * 12) + "/etc/passwd"
    with pytest.raises(cf.FileOpError) as exc:
        cf._resolve_path(sneaky, must_exist=False)
    assert "硬拦截" in str(exc.value)


def test_directory_is_not_a_file(workspace):
    with pytest.raises(cf.FileOpError) as exc:
        cf._resolve_path(str(workspace), must_exist=True)
    assert "目录" in str(exc.value)


# ============================================================ read_file


async def test_read_file_returns_whole_small_file(workspace):
    target = _write(workspace / "notes.md", "第一行\n第二行\n第三行\n")
    out = await cf.read_file({"path": str(target)})
    assert out["ok"] is True
    assert out["total_lines"] == 3
    assert out["content"] == "第一行\n第二行\n第三行"
    assert out["has_more"] is False
    assert out["size"] == target.stat().st_size


async def test_read_file_pages_with_offset_and_limit(workspace):
    body = "\n".join(f"line{i}" for i in range(1, 501)) + "\n"
    target = _write(workspace / "big.txt", body)

    first = await cf.read_file({"path": str(target), "offset": 1, "limit": 10})
    assert first["content"].splitlines() == [f"line{i}" for i in range(1, 11)]
    assert first["has_more"] is True

    tail = await cf.read_file({"path": str(target), "offset": 495, "limit": 100})
    assert tail["content"].startswith("line495")
    assert tail["has_more"] is False


async def test_read_file_caps_bytes_and_says_so(workspace):
    target = _write(workspace / "long.txt", "x" * 5000 + "\n")
    out = await cf.read_file({"path": str(target), "max_bytes": 100})
    assert out["content_truncated_by_bytes"] is True
    assert len(out["content"]) == 100


async def test_read_file_refuses_binary_instead_of_returning_garbage(workspace):
    target = workspace / "blob.bin"
    target.write_bytes(b"\x00\x01\x02binary")
    out = await cf.read_file({"path": str(target)})
    assert out["ok"] is False and out["binary"] is True
    assert "二进制" in out["error"]


@pytest.mark.parametrize("params", [
    {"path": ""},
    {"path": "missing.txt"},
])
async def test_read_file_reports_missing_path_rather_than_empty_content(workspace,
                                                                       params):
    with pytest.raises(cf.FileOpError):
        await cf.read_file(params)


@pytest.mark.parametrize("key", ["offset", "limit", "max_bytes"])
async def test_read_file_rejects_non_numeric_window(workspace, key):
    target = _write(workspace / "a.txt", "1\n")
    with pytest.raises(cf.FileOpError) as exc:
        await cf.read_file({"path": str(target), key: "很多"})
    assert key in str(exc.value)


# ============================================================ write_file


async def test_write_file_creates_parents_and_writes(workspace):
    target = workspace / "deep" / "nested" / "new.py"
    out = await cf.write_file({"path": str(target), "content": "print('hi')\n"})
    assert out["ok"] is True and out["existed"] is False
    assert out["backup_path"] is None
    assert target.read_text(encoding="utf-8") == "print('hi')\n"
    assert out["added_lines"] == 1


async def test_write_file_backs_up_before_overwriting(workspace):
    target = _write(workspace / "cfg.txt", "old-a\nold-b\n")
    out = await cf.write_file({"path": str(target), "content": "old-a\nnew-b\n"})

    assert out["changed"] is True
    backup = Path(out["backup_path"])
    assert backup.is_file(), "覆盖前必须留下原件"
    assert backup.read_text(encoding="utf-8") == "old-a\nold-b\n"
    assert target.read_text(encoding="utf-8") == "old-a\nnew-b\n"
    assert out["added_lines"] == 1 and out["removed_lines"] == 1
    assert out["hunks"] == 1
    assert "-old-b" in out["diff"] and "+new-b" in out["diff"]


async def test_write_file_dry_run_has_zero_side_effects(workspace):
    target = _write(workspace / "keep.txt", "original\n")
    out = await cf.write_file({"path": str(target), "content": "replaced\n",
                               "dry_run": True})
    assert out["dry_run"] is True and out["changed"] is True
    assert "-original" in out["diff"]
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not (workspace.parent / "backups").exists()


async def test_write_file_skips_identical_content_without_backup(workspace):
    target = _write(workspace / "same.txt", "unchanged\n")
    out = await cf.write_file({"path": str(target), "content": "unchanged\n"})
    assert out["changed"] is False and out["backup_path"] is None
    assert out["diff"] == ""


async def test_write_file_allows_emptying_a_file(workspace):
    """清空是合法意图，`content=""` 不能被当成"参数缺失"。"""
    target = _write(workspace / "log.txt", "noise\n")
    out = await cf.write_file({"path": str(target), "content": ""})
    assert out["changed"] is True and target.stat().st_size == 0
    assert out["removed_lines"] == 1


async def test_write_file_rejects_non_string_content(workspace):
    with pytest.raises(cf.FileOpError):
        await cf.write_file({"path": str(workspace / "x.txt"), "content": 123})


async def test_write_file_refuses_oversized_content(workspace, monkeypatch):
    monkeypatch.setattr(cf, "MAX_WRITE_BYTES", 64)
    with pytest.raises(cf.FileOpError) as exc:
        await cf.write_file({"path": str(workspace / "big.txt"),
                             "content": "y" * 200})
    assert "上限" in str(exc.value)


async def test_write_file_keeps_existing_file_untouched_when_backup_fails(
        workspace, monkeypatch):
    """备份失败必须放弃写入——「改坏了找不到原件」比这次没改成严重。"""
    target = _write(workspace / "important.txt", "keep me\n")

    def _boom() -> Path:
        raise OSError("备份盘满了")

    monkeypatch.setattr(cf, "_backup_root", _boom)
    with pytest.raises(cf.FileOpError):
        await cf.write_file({"path": str(target), "content": "overwrite\n"})
    assert target.read_text(encoding="utf-8") == "keep me\n"


async def test_write_file_refuses_the_env_file(workspace):
    target = workspace / ".env"
    with pytest.raises(cf.FileOpError):
        await cf.write_file({"path": str(target), "content": "JWT_SECRET=x\n"})
    assert not target.exists()


# ============================================================ apply_patch


async def test_apply_patch_replaces_unique_anchor(workspace):
    target = _write(workspace / "svc.py",
                    "def run():\n    return 1\n\n\ndef stop():\n    return 0\n")
    out = await cf.apply_patch({"path": str(target), "edits": [
        {"old": "    return 1", "new": "    return 2"}]})
    assert out["ok"] is True and out["changed"] is True
    assert "    return 2" in target.read_text(encoding="utf-8")
    assert Path(out["backup_path"]).read_text(
        encoding="utf-8").startswith("def run():")
    assert out["added_lines"] == 1 and out["removed_lines"] == 1


async def test_apply_patch_preserves_crlf_line_endings(workspace):
    """换行归一化会把整份文件改成"看起来没变、diff 全是改动"的大事故。"""
    target = workspace / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    out = await cf.apply_patch({"path": str(target),
                               "edits": [{"old": "beta", "new": "BETA"}]})
    assert out["ok"] is True
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"
    assert out["hunks"] == 1


async def test_apply_patch_fails_loudly_when_anchor_is_missing(workspace):
    target = _write(workspace / "a.py", "x = 1\n")
    with pytest.raises(cf.FileOpError) as exc:
        await cf.apply_patch({"path": str(target),
                             "edits": [{"old": "x = 999", "new": "x = 2"}]})
    assert "定位失败" in str(exc.value)
    assert target.read_text(encoding="utf-8") == "x = 1\n"


async def test_apply_patch_refuses_ambiguous_anchor(workspace):
    target = _write(workspace / "dup.txt", "same\nsame\n")
    with pytest.raises(cf.FileOpError) as exc:
        await cf.apply_patch({"path": str(target),
                             "edits": [{"old": "same", "new": "other"}]})
    assert "歧义" in str(exc.value) and "2 处" in str(exc.value)
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


async def test_apply_patch_replace_all_hits_every_occurrence(workspace):
    target = _write(workspace / "dup.txt", "same\nsame\nsame\n")
    out = await cf.apply_patch({"path": str(target), "edits": [
        {"old": "same", "new": "other", "replace_all": True}]})
    assert out["ok"] is True
    assert target.read_text(encoding="utf-8") == "other\nother\nother\n"
    assert out["edits"][0]["occurrences"] == 3


async def test_one_bad_edit_aborts_the_whole_batch(workspace):
    """补丁必须全成或全不成：改了一半的文件比没改更难排查。"""
    target = _write(workspace / "svc.py", "a = 1\nb = 2\n")
    with pytest.raises(cf.FileOpError):
        await cf.apply_patch({"path": str(target), "edits": [
            {"old": "a = 1", "new": "a = 10"},
            {"old": "b = 99", "new": "b = 20"}]})
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"


async def test_apply_patch_dry_run_previews_without_writing(workspace):
    target = _write(workspace / "svc.py", "a = 1\n")
    out = await cf.apply_patch({"path": str(target), "dry_run": True,
                               "edits": [{"old": "a = 1", "new": "a = 2"}]})
    assert out["dry_run"] is True and out["changed"] is True
    assert "+a = 2" in out["diff"] and "-a = 1" in out["diff"]
    assert target.read_text(encoding="utf-8") == "a = 1\n"
    assert not (workspace.parent / "backups").exists()


async def test_apply_patch_deletes_with_empty_new(workspace):
    target = _write(workspace / "l.txt", "keep\nbye\n")
    out = await cf.apply_patch({"path": str(target),
                               "edits": [{"old": "bye\n", "new": ""}]})
    assert out["ok"] is True
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("edits", [
    None, [], "old->new", [{}], [{"old": "", "new": "x"}],
    [{"old": "x"}], [{"old": "x", "new": None}], [{"old": 1, "new": "x"}],
])
async def test_apply_patch_rejects_malformed_edits(workspace, edits):
    target = _write(workspace / "a.py", "x = 1\n")
    with pytest.raises(cf.FileOpError):
        await cf.apply_patch({"path": str(target), "edits": edits})
    assert target.read_text(encoding="utf-8") == "x = 1\n"


async def test_apply_patch_refuses_non_utf8_file(workspace):
    """按 errors=replace 读进来再写出去就是毁文件，宁可拒绝。"""
    target = workspace / "gbk.txt"
    target.write_bytes("中文".encode("gb18030"))
    with pytest.raises(cf.FileOpError) as exc:
        await cf.apply_patch({"path": str(target),
                             "edits": [{"old": "中文", "new": "english"}]})
    assert "UTF-8" in str(exc.value)


async def test_apply_patch_needs_an_existing_file(workspace):
    with pytest.raises(cf.FileOpError):
        await cf.apply_patch({"path": str(workspace / "ghost.py"),
                             "edits": [{"old": "a", "new": "b"}]})


# ============================================================ 注册与分类


def test_file_executors_are_registered_once_in_the_single_checkpoint():
    for cap in ("read_file", "write_file", "apply_patch"):
        assert cap in computer_service.EXTENDED_CAPABILITIES
        assert callable(cf.FILE_EXECUTORS[cap])


def test_write_caps_are_high_risk_and_read_is_not():
    assert {"read_file", "write_file", "apply_patch"} <= set(
        computer_service.EXTENDED_CAPABILITIES)
    assert "write_file" in computer_service.HIGH_RISK_CAPABILITIES
    assert "apply_patch" in computer_service.HIGH_RISK_CAPABILITIES
    assert "read_file" not in computer_service.HIGH_RISK_CAPABILITIES


@pytest.mark.parametrize("cap", ["read_file", "write_file", "apply_patch"])
def test_read_is_observe_and_writes_need_approval(cap):
    row = ComputerPolicy(company_id="co", mode="default")
    verdict = policy.evaluate_action(row, cap, {"path": "/tmp/a.txt"})
    if cap == "read_file":
        assert verdict["decision"] == "allow" and verdict["matched"] == "observe"
    else:
        assert verdict["decision"] == "approval"


def test_auto_mode_only_lets_writes_inside_the_dir_whitelist():
    row = ComputerPolicy(company_id="co", mode="auto",
                         auto_dirs=["/Users/boss/project"],
                         auto_commands=[], auto_apps=[])
    inside = policy.evaluate_action(row, "apply_patch",
                                   {"path": "/Users/boss/project/a.py"})
    outside = policy.evaluate_action(row, "write_file",
                                     {"path": "/Users/boss/other/a.py"})
    assert inside["decision"] == "allow" and "目录白名单" in inside["reason"]
    assert outside["decision"] == "approval"


def test_hard_block_survives_every_mode():
    """白名单再宽、模式再松，凭据路径都不放行。"""
    row = ComputerPolicy(company_id="co", mode="full",
                         full_control_until=None)
    for cap in ("read_file", "write_file", "apply_patch"):
        verdict = policy.evaluate_action(row, cap,
                                        {"path": "/Users/boss/.ssh/id_rsa"})
        assert verdict["decision"] == "blocked", cap


@pytest.mark.parametrize("cap,params,expected", [
    ("read_file", {}, "path 不能为空"),
    ("write_file", {"path": "a.txt"}, "write_file 需要 content"),
    ("apply_patch", {"path": "a.txt"}, "apply_patch 需要 edits"),
    ("apply_patch", {"path": "a.txt", "edits": [{"old": "", "new": "x"}]},
     "第 1 处 edits 不合法"),
])
def test_precheck_catches_missing_params_before_touching_disk(cap, params,
                                                              expected):
    out = computer_service._precheck_capability(cap, params)
    assert out is not None and out["precheck"] is True
    assert expected in out["error"]


def test_precheck_passes_valid_file_params():
    assert computer_service._precheck_capability(
        "read_file", {"path": "apps/api/app/main.py"}) is None
    assert computer_service._precheck_capability(
        "apply_patch", {"path": "a.py",
                        "edits": [{"old": "a", "new": "b"}]}) is None


# ============================================================ 执行服务接线


async def test_execute_service_blocks_credential_write_after_grant(client,
                                                                   tenants,
                                                                   monkeypatch):
    """授权给了也不给碰凭据：红线在执行服务这一层再问一次。"""
    cid = tenants.alice_co["id"]
    ran: list[str] = []

    async def _spy(params: dict) -> dict:
        ran.append("ran")
        return {"ok": True}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "write_file", _spy)
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability="write_file", granted=True))
        await s.commit()

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="write_file",
            params={"path": "/Users/boss/.ssh/authorized_keys",
                    "content": "ssh-rsa AAAA\n"})
    assert ran == []
    assert outcome["ok"] is False and outcome["decision"] == "blocked"


async def test_execute_service_runs_granted_read_through_the_executor(
        client, tenants, workspace, monkeypatch):
    cid = tenants.alice_co["id"]
    target = _write(workspace / "report.txt", "本月营收 12 万\n")
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability="read_file", granted=True))
        s.add(ComputerPolicy(company_id=cid, mode="default", auto_dirs=[],
                             auto_commands=[], auto_apps=[],
                             notify_on_finish=True))
        await s.commit()

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="read_file",
            params={"path": str(target)})
    assert outcome["ok"] is True, outcome
    assert "12 万" in outcome["result"]["content"]
