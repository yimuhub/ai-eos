"""设备端 sidecar 的本机护栏与执行。

sidecar 是「第二把锁」：后端的策略管该不该做，设备自己管允不允许。
所以这里的测试全部围绕**本地拒绝**——服务端放行了，本地仍然拦得住，
才说明远程设备不是"服务器说啥就是啥"的傀儡。

只测纯函数与临时目录里的真实读写，不连服务端、不碰仓库代码。
"""
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[3] / "apps" / "device_agent" / "aieos_sidecar.py"


def _load():
    spec = importlib.util.spec_from_file_location("aieos_sidecar", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sidecar = _load()


# ============================================================ 路径护栏


def test_credential_and_system_paths_are_never_allowed(tmp_path):
    secret_home = tmp_path / "home"
    (secret_home / ".ssh").mkdir(parents=True)
    key = secret_home / ".ssh" / "id_rsa"
    key.write_text("PRIVATE KEY")
    etc = secret_home / ".env"
    etc.write_text("API_TOKEN=abc")

    assert sidecar.path_denied(key)
    assert sidecar.path_denied(etc)
    # 归一化之前的绕过写法：`~/.ssh/../.ssh/id_rsa` 还是那个私钥
    assert sidecar.path_denied(f"{secret_home}/.ssh/../.ssh/id_rsa")
    assert sidecar.path_denied("/etc/passwd")
    assert sidecar.path_denied("/System/Library/Fonts/Helvetica.ttc")
    # 普通项目路径不受影响
    assert sidecar.path_denied(tmp_path / "repo" / "app.py") is None


def test_writes_need_an_explicit_writable_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    # 一个目录都没配 = 这台设备只读
    assert "没开放任何可写目录" in sidecar.path_writable(project / "a.py", [])
    assert sidecar.path_writable(project / "a.py", [str(project)]) is None
    assert "不在本机允许写入的目录里" in sidecar.path_writable(
        outside / "a.py", [str(project)])
    # 可写根目录不能把红条目录罩进去：`--allow-root ~` 不该放开 ~/.ssh
    assert sidecar.path_writable(secret := str(project / ".git" / "config"),
                                 [str(project)])
    assert "密钥/版本库红条" in sidecar.path_writable(secret, [str(project)])


# ============================================================ 命令护栏


def test_read_only_commands_pass_and_writers_are_refused(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    roots = [str(project)]

    for cmd in ("ls -la", "grep -rn 调用点 apps/api", "cat a.txt",
                "git status && git diff", "find . -name '*.py'"):
        assert sidecar.check_command(cmd, roots) is None, cmd

    assert "不是只读子命令" in sidecar.check_command("git push origin main", roots)
    assert "破坏性片段" in sidecar.check_command("rm -rf /", roots)
    assert "不在本机只读命令白名单里" in sidecar.check_command("make build", roots)
    assert "内联代码" in sidecar.check_command("python3 -c 'import os;os.system(1)'", roots)
    assert "替换/展开" in sidecar.check_command("echo $(cat ~/.ssh/id_rsa)", roots)
    assert "会写文件/起进程" in sidecar.check_command("sed -i s/a/b/ f.txt", roots)
    assert "会写文件/起进程" in sidecar.check_command("find . -delete", roots)
    # 老板显式放行的命令只剩其他规则拦着
    assert sidecar.check_command("make build", roots, extra_commands=["make"]) is None


def test_output_redirection_is_guarded_like_a_write(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    roots = [str(project)]

    # 相对路径按「命令真正的工作目录」判，和 shell 的执行目录对齐
    assert sidecar.check_command("ls > inside.txt", roots,
                                 cwd=str(project)) is None
    assert "重定向被拦" in sidecar.check_command("ls > /etc/hosts", roots,
                                          cwd=str(project))
    assert "重定向被拦" in sidecar.check_command("ls > ../outside.txt", roots,
                                          cwd=str(project))
    # 同一条命令换个执行目录就必须拦：护栏跟着执行目录走，不是猜的
    assert "重定向被拦" in sidecar.check_command("ls > inside.txt", roots,
                                          cwd=str(tmp_path))


def test_run_shell_relative_redirect_lands_in_the_writable_root(tmp_path,
                                                                isolated_dirs):
    """护栏与执行共用一个工作目录，否则 `ls > out.txt` 判的是一处、写的是另一处。"""
    project = tmp_path / "project"
    project.mkdir()
    out = sidecar.run_shell({"command": "echo hi > note.txt"}, _cfg(project))
    assert out["ok"] is True, out
    assert (project / "note.txt").read_text(encoding="utf-8").strip() == "hi"

    with pytest.raises(PermissionError) as exc:
        sidecar.run_shell({"command": "echo hi > note.txt",
                           "cwd": str(tmp_path)}, _cfg(project))
    assert "重定向被拦" in str(exc.value)


# ============================================================ 文件执行


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """把配置与备份目录都挪进 tmp_path：测试不能往真 ~/.aieos 里写。"""
    config = tmp_path / "cfg" / "sidecar.json"
    config.parent.mkdir()
    monkeypatch.setattr(sidecar, "CONFIG_PATH", config)
    monkeypatch.setattr(sidecar, "BACKUP_DIR", tmp_path / "backups")
    return config


def _cfg(project: Path) -> dict:
    return {"allow_roots": [str(project)], "allow_commands": []}


def test_write_file_backs_up_and_reports_diff(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "app.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

    out = sidecar.write_file({"path": str(target),
                              "content": "def f():\n    return 2\n"},
                             _cfg(project))
    assert out["ok"] is True and out["added_lines"] == 1 and out["removed_lines"] == 1
    assert "return 1" in out["diff"] and "return 2" in out["diff"]
    assert target.read_text(encoding="utf-8").endswith("return 2\n")
    backup = Path(out["backup"])
    assert backup.exists() and "return 1" in backup.read_text(encoding="utf-8")

    created = sidecar.write_file({"path": str(project / "new.py"),
                                  "content": "x = 1\n"}, _cfg(project))
    assert created["created"] is True and created["backup"] is None


def test_dry_run_writes_nothing(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "a.txt"
    target.write_text("old\n", encoding="utf-8")

    out = sidecar.write_file({"path": str(target), "content": "new\n",
                              "dry_run": True}, _cfg(project))
    assert out["dry_run"] is True and out["ok"] is True
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not sidecar.BACKUP_DIR.exists()


def test_patch_is_all_or_nothing(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "svc.py"
    original = "A = 1\nB = 2\nC = 3\n"
    target.write_text(original, encoding="utf-8")

    bad = {"path": str(target), "edits": [{"old": "A = 1", "new": "A = 9"},
                                          {"old": "没有这段", "new": "X"}]}
    with pytest.raises(ValueError, match="第 2 处锚点"):
        sidecar.apply_patch(bad, _cfg(project))
    assert target.read_text(encoding="utf-8") == original, "失败的补丁必须一个字都不改"

    ambiguous = {"path": str(target), "edits": [{"old": "= ", "new": "= "}]}
    with pytest.raises(ValueError, match="出现 3 次"):
        sidecar.apply_patch(ambiguous, _cfg(project))

    good = {"path": str(target), "edits": [{"old": "B = 2", "new": "B = 20"},
                                           {"old": "C = 3", "new": "C = 30"}]}
    out = sidecar.apply_patch(good, _cfg(project))
    assert out["ok"] is True and len(out["edits_applied"]) == 2
    assert target.read_text(encoding="utf-8") == "A = 1\nB = 20\nC = 30\n"


def test_binary_and_non_utf8_files_are_not_touched(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    project.mkdir()
    blob = project / "raw.bin"
    blob.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError, match="二进制"):
        sidecar.read_file({"path": str(blob)}, _cfg(project))

    gbk = project / "cn.txt"
    gbk.write_bytes("中文".encode("gbk"))
    with pytest.raises(ValueError, match="UTF-8"):
        sidecar.read_file({"path": str(gbk)}, _cfg(project))


def test_read_file_pages_and_lists_dirs(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "b.py").write_text("1\n2\n3\n", encoding="utf-8")

    listing = sidecar.read_file({"path": str(project / "src")}, _cfg(project))
    assert listing["is_dir"] is True and listing["entries"] == ["b.py"]

    paged = sidecar.read_file({"path": str(project / "src" / "b.py"),
                               "offset": 1, "limit": 1}, _cfg(project))
    assert paged["lines"] == ["2\n"] and paged["total_lines"] == 3
    assert paged["truncated"] is True


def test_relative_paths_follow_the_writable_root(tmp_path, isolated_dirs):
    """老板在页面上写「线上问题清单.md」，指的是那台机器上开放的那个目录。

    以前只 expanduser：文件操作按 sidecar 的**进程目录**找，报「文件不存在」，
    而同一个相对路径在 run_shell 里却是好用的——两条路一个基准。
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("P0\n", encoding="utf-8")
    cfg = _cfg(project)

    got = sidecar.read_file({"path": "notes.md"}, cfg)
    assert got["lines"] == ["P0\n"], got
    assert got["path"] == str(project / "notes.md")

    # 基准同时给了护栏：越界的相对写法照样拦，而不是「找不到文件」蒙混过去
    with pytest.raises(PermissionError, match="不在本机允许写入的目录里"):
        sidecar.write_file({"path": "../escape.txt", "content": "x"}, cfg)
    assert not (tmp_path / "escape.txt").exists()


# ============================================================ 执行与回报


def test_execute_refuses_unknown_capability(tmp_path, isolated_dirs):
    with pytest.raises(ValueError, match="不会执行"):
        sidecar.execute({"capability": "screenshot", "params": {},
                         "dispatch_id": "d1"}, _cfg(tmp_path))


def test_local_denials_surface_as_exceptions_for_the_loop_to_report(tmp_path,
                                                                   isolated_dirs):
    """护栏拒绝走异常冒到主循环，再由循环分类成 ok=False + denied_locally 回报。

    关键是"回报得带上本地拦住的原因"：否则老板只看到命令消失了，
    分不清是设备没跑、网络断了，还是这台机器自己不允许。
    """
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(PermissionError, match="系统目录"):
        sidecar.execute({"capability": "write_file", "dispatch_id": "d1",
                         "params": {"path": "/etc/hosts", "content": "x"}},
                        _cfg(project))
    with pytest.raises(PermissionError, match="没开放任何可写目录"):
        sidecar.execute({"capability": "write_file", "dispatch_id": "d2",
                         "params": {"path": str(project / "a.py"),
                                    "content": "x"}}, {"allow_roots": []})


def test_run_shell_executes_whitelisted_command(tmp_path, isolated_dirs):
    project = tmp_path / "project"
    project.mkdir()
    out = sidecar.run_shell({"command": "echo hi", "cwd": str(project)},
                            _cfg(project))
    assert out["ok"] is True and out["stdout"].strip() == "hi"
    assert out["exit_code"] == 0

    refused = sidecar.check_command("curl http://example.com", [str(project)])
    assert refused and "白名单" in refused
