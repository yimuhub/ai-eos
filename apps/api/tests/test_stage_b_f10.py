"""Stage B F10：文件整理意图、两轮预览确认与业务意图优先级。"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from pathlib import Path

import pytest

import app.nl_computer as nl_computer


def _parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def _safe_backup(root: Path, expected: bytes = b"new") -> Path:
    backups = list(root.glob(".aieos-organize-*"))
    assert backups
    safe = [path for path in backups
            if path.is_file() and not path.is_symlink()
            and path.read_bytes() == expected]
    assert safe, "公司根内必须保留至少一份内容正确的安全副本"
    return safe[0]


@pytest.fixture
def allow_capability(monkeypatch):
    async def allow(*args, **kwargs):
        return None

    monkeypatch.setattr(nl_computer, "_require_computer_permission", allow)


async def _make_agent(client, user, company) -> dict:
    r = await client.post("/agents", json={
        "company_id": company["id"], "name": "整理助手", "role": "运营"},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _stream(client, user, agent_id: str, message: str) -> dict:
    r = await client.post(f"/agents/{agent_id}/chat/stream",
                          json={"message": message}, headers=user["headers"])
    assert r.status_code == 200, r.text
    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    return done


@pytest.mark.parametrize("text", [
    "整理计划", "清理思路", "收拾项目方案", "整理", "整理文件",
    "整理这个文件夹", "分类任务清单",
    "整理 2026/11/20 的项目计划", "整理 2026-11-20 的项目计划文件夹",
    "整理 https://example.com/2026/11/20 的项目计划",
    "清理 2026/11/20 的报告文件夹", "整理 www.example.com/reports 文件夹",
    "整理 example.com/reports 文件夹", "整理 192.168.1.10/reports 文件夹",
])
def test_business_or_unspecified_phrases_never_trigger_organization(text):
    assert nl_computer.detect_intent(text) is None


def test_documented_relative_path_is_repository_relative_and_tenant_scoped(
        monkeypatch, tmp_path):
    import app.core.sandbox as sandbox

    repo = tmp_path / "repo"
    shared = repo / "storage"
    shared.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "PROJECT_ROOT", repo)
    monkeypatch.setattr(nl_computer, "storage_root", lambda: shared)

    target = nl_computer.resolve_organize_target("storage/reports", "company-a")
    assert target == (shared / "companies" / "company-a" / "reports").resolve()
    assert "storage/storage" not in str(target)
    assert nl_computer.resolve_organize_target("reports", "company-a") == target
    assert nl_computer.resolve_organize_target("storage/reports", "company-b") != target

    assert nl_computer.detect_intent(
        "整理 example.com/reports，再整理 storage/reports 文件夹"
    )["params"]["path"] == "storage/reports"
    assert nl_computer.detect_intent(
        "整理 storage/reports 文件夹，参考 https://example.com/a/b"
    )["params"]["path"] == "storage/reports"

    # 共享根、别家公司绝对路径、遍历与 URL 都不能借路径解析越界。
    from app.core.sandbox import SandboxViolation
    for raw in (shared, shared / "reports", target.parent.parent / "company-b" / "reports",
                "storage/../../etc", "https://example.com/reports",
                "example.com/reports", "192.168.1.10/reports"):
        try:
            nl_computer.resolve_organize_target(raw, "company-a")
        except SandboxViolation:
            pass
        else:
            raise AssertionError(f"unsafe target accepted: {raw}")


async def test_organize_cannot_touch_another_company_or_shared_storage(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    own = nl_computer.company_storage_root("company-a") / "reports"
    other = nl_computer.company_storage_root("company-b") / "reports"
    shared = tmp_path / "reports"
    for path in (own, other, shared):
        path.mkdir(parents=True)
        (path / "report.pdf").write_bytes(b"x")

    denied = await nl_computer.preview_organize_folder(other, "company-a")
    assert denied["ok"] is False and denied.get("denied") is True
    denied_shared = await nl_computer.preview_organize_folder(shared, "company-a")
    assert denied_shared["ok"] is False and denied_shared.get("denied") is True
    assert (other / "report.pdf").is_file() and (shared / "report.pdf").is_file()

    preview = await nl_computer.preview_organize_folder(own, "company-a")
    assert preview["ok"] is True, preview
    result = await nl_computer.execute_organize_preview(
        own, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-a")
    assert result["ok"] is True, result
    assert (own / "文档" / "report.pdf").is_file()
    assert not list(own.parent.glob(".aieos-organize-*"))
    assert (other / "report.pdf").is_file() and (shared / "report.pdf").is_file()


def test_company_root_and_nested_symlinks_cannot_escape(monkeypatch, tmp_path):
    from app.core.sandbox import SandboxViolation

    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    companies = tmp_path / "companies"
    companies.mkdir()
    own = companies / "company-a"
    other = companies / "company-b"
    own.mkdir()
    assert nl_computer.company_storage_root("company-b") == other
    outside = tmp_path / "outside"
    outside.mkdir()

    # 公司根本身不能指向另一家公司或共享 storage 外的目录。
    (companies / "company-c").symlink_to(other, target_is_directory=True)
    (companies / "company-d").symlink_to(outside, target_is_directory=True)
    for cid in ("company-c", "company-d"):
        try:
            nl_computer.company_storage_root(cid)
        except SandboxViolation:
            pass
        else:
            raise AssertionError(f"公司根符号链接越界: {cid}")

    # 公司内分类目录被替换成别家公司/外部符号链接时同样拒绝。
    (own / "文档").symlink_to(other, target_is_directory=True)
    try:
        nl_computer.resolve_organize_target(own / "文档", "company-a")
    except SandboxViolation:
        pass
    else:
        raise AssertionError("分类目录跨公司符号链接被接受")

    # 整个 companies 容器被符号链接重定向时同样拒绝。
    outside_companies = tmp_path / "outside-companies"
    outside_companies.mkdir()
    companies.rename(tmp_path / "original-companies")
    (tmp_path / "companies").symlink_to(outside_companies, target_is_directory=True)
    try:
        nl_computer.company_storage_root("company-a")
    except SandboxViolation:
        pass
    else:
        raise AssertionError("companies 容器符号链接被接受")


async def test_organize_execution_never_overwrites_destination_created_after_preview(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace

    def create_collision_then_rename(source_fd, source_name, target_fd, target_name):
        destination = target / "文档" / "report.pdf"
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(b"existing")
        return real_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", create_collision_then_rename)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert source.read_bytes() == b"new"
    assert (target / "文档" / "report.pdf").read_bytes() == b"existing"


async def test_organize_execution_rejects_category_symlink_swap_after_preview(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_move = nl_computer._move_organize_files

    def swap_category_then_move(*args, **kwargs):
        (target / "文档").symlink_to(outside, target_is_directory=True)
        return real_move(*args, **kwargs)

    monkeypatch.setattr(nl_computer, "_move_organize_files", swap_category_then_move)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert source.read_bytes() == b"new"
    assert list(outside.iterdir()) == []


async def test_organize_execution_restores_file_when_category_directory_moves_outside(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace

    def move_category_then_rename(source_fd, source_name, target_fd, target_name):
        category = target / "文档"
        if category.exists():
            category.rename(outside / "文档")
        return real_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", move_category_then_rename)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "文档").iterdir()) == []


async def test_organize_execution_restores_file_when_target_directory_moves_outside(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace
    moved = False

    def move_target_then_rename(source_fd, source_name, target_fd, target_name):
        nonlocal moved
        if not moved:
            moved = True
            target.rename(outside / "reports")
        return real_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", move_target_then_rename)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "reports" / "文档").iterdir()) == []


async def test_organize_failure_recovers_when_root_backup_is_replaced_before_restore(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace
    real_restore = nl_computer._restore_organize_file
    moved = False

    def move_category_then_rename(source_fd, source_name, target_fd, target_name):
        nonlocal moved
        if not moved:
            moved = True
            (target / "文档").rename(outside / "文档")
        return real_rename(source_fd, source_name, target_fd, target_name)

    def replace_backup_then_restore(*args, **kwargs):
        backup = next(target.parent.glob(".aieos-organize-*"))
        backup.unlink()
        backup.write_bytes(b"attacker")
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", move_category_then_rename)
    monkeypatch.setattr(nl_computer, "_restore_organize_file",
                        replace_backup_then_restore)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "文档").iterdir()) == []


async def test_organize_success_rechecks_after_backup_removal_and_recovers_move(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_unlink = nl_computer.os.unlink
    attacked = False

    def move_category_then_unlink(path, *args, **kwargs):
        nonlocal attacked
        if (not attacked and isinstance(path, str)
                and path.startswith(".aieos-organize-")
                and kwargs.get("dir_fd") is not None):
            attacked = True
            (target / "文档").rename(outside / "文档")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(nl_computer.os, "unlink", move_category_then_unlink)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert attacked is True
    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "文档").iterdir()) == []


async def test_organize_recovery_does_not_require_backup_recreation(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_unlink = nl_computer.os.unlink
    real_create_backup = nl_computer._create_organize_backup
    real_restore = nl_computer._restore_organize_file
    moved = False
    backup_calls = 0
    restore_calls = 0

    def move_category_then_unlink(path, *args, **kwargs):
        nonlocal moved
        if (not moved and isinstance(path, str)
                and path.startswith(".aieos-organize-")
                and kwargs.get("dir_fd") is not None):
            moved = True
            (target / "文档").rename(outside / "文档")
        return real_unlink(path, *args, **kwargs)

    def fail_backup_recreation(*args, **kwargs):
        nonlocal backup_calls
        backup_calls += 1
        if backup_calls == 2:
            raise OSError(errno.EIO, "injected backup recreation failure")
        return real_create_backup(*args, **kwargs)

    def count_restore(*args, **kwargs):
        nonlocal restore_calls
        restore_calls += 1
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(nl_computer.os, "unlink", move_category_then_unlink)
    monkeypatch.setattr(nl_computer, "_create_organize_backup",
                        fail_backup_recreation)
    monkeypatch.setattr(nl_computer, "_restore_organize_file", count_restore)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert moved is True
    assert backup_calls == 1
    assert restore_calls == 1
    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "文档").iterdir()) == []


async def test_organize_inter_file_category_swap_cannot_report_success(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "a.pdf").write_bytes(b"a")
    (target / "b.pdf").write_bytes(b"b")
    outside = tmp_path / "outside"
    outside.mkdir()

    real_create_backup = nl_computer._create_organize_backup
    backup_calls = 0

    def move_category_before_second_backup(*args, **kwargs):
        nonlocal backup_calls
        backup_calls += 1
        if backup_calls == 2:
            (target / "文档").rename(outside / "文档")
        return real_create_backup(*args, **kwargs)

    monkeypatch.setattr(nl_computer, "_create_organize_backup",
                        move_category_before_second_backup)
    with pytest.raises(OSError, match="移出公司沙箱"):
        await nl_computer.organize_folder(target, company_id="company-race")

    safe = [path for path in target.parent.glob(".aieos-organize-*")
            if path.is_file() and not path.is_symlink()]
    assert {path.read_bytes() for path in safe} >= {b"a", b"b"}
    assert list((outside / "文档").iterdir()) == []
    assert list((target / "文档").iterdir()) == []


async def test_organize_failed_rename_keeps_backup_when_target_moves_outside(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace
    collision = outside / "reports" / "文档" / "report.pdf"

    def move_target_and_create_collision(source_fd, source_name, target_fd,
                                         target_name):
        target.rename(outside / "reports")
        collision.write_bytes(b"existing")
        return real_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(nl_computer, "_rename_noreplace",
                        move_target_and_create_collision)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert (outside / "reports" / "report.pdf").read_bytes() == b"new"
    assert collision.read_bytes() == b"existing"


async def test_organize_failure_keeps_root_backup_when_restore_directory_is_moved_again(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace
    real_restore = nl_computer._restore_organize_file
    moved = False

    def move_target_then_rename(source_fd, source_name, target_fd, target_name):
        nonlocal moved
        if not moved:
            moved = True
            target.rename(outside / "reports")
        return real_rename(source_fd, source_name, target_fd, target_name)

    def move_restore_directory_again(*args, **kwargs):
        target.mkdir()
        target.rename(outside / "restored-reports")
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", move_target_then_rename)
    monkeypatch.setattr(nl_computer, "_restore_organize_file",
                        move_restore_directory_again)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "reports" / "文档").iterdir()) == []
    assert list((outside / "restored-reports").iterdir()) == []


async def test_organize_execution_restores_file_when_category_path_is_replaced_by_symlink(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    outside = tmp_path / "outside"
    outside.mkdir()
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    real_rename = nl_computer._rename_noreplace
    swapped = False

    def move_category_and_replace_path_then_rename(
            source_fd, source_name, target_fd, target_name):
        nonlocal swapped
        category = target / "文档"
        if not swapped and category.exists():
            swapped = True
            category.rename(outside / "文档")
            category.symlink_to(outside / "文档", target_is_directory=True)
        return real_rename(source_fd, source_name, target_fd, target_name)

    monkeypatch.setattr(nl_computer, "_rename_noreplace", move_category_and_replace_path_then_rename)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False, result
    assert _safe_backup(target.parent).read_bytes() == b"new"
    assert list((outside / "文档").iterdir()) == []


def test_fd_path_check_rejects_directory_renamed_during_path_resolution(
        monkeypatch, tmp_path):
    expected = tmp_path / "expected"
    expected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    fd = os.open(expected, nl_computer._DIR_FLAGS)
    real_resolve = Path.resolve
    attacked = False

    def resolve_with_directory_swap(path, *args, **kwargs):
        nonlocal attacked
        if path == outside / "expected" and not attacked:
            attacked = True
            path.rename(outside / "hidden")
            path.symlink_to(expected, target_is_directory=True)
        return real_resolve(path, *args, **kwargs)

    try:
        expected.rename(outside / "expected")
        monkeypatch.setattr(Path, "resolve", resolve_with_directory_swap)
        with pytest.raises(OSError):
            nl_computer._assert_fd_path(fd, expected, "分类目录")
    finally:
        os.close(fd)


def test_fd_path_check_rejects_parent_symlink_redirection(monkeypatch, tmp_path):
    root = tmp_path / "root"
    nested = root / "reports"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    root_fd = os.open(root, nl_computer._DIR_FLAGS)
    nested_fd = os.open(nested, nl_computer._DIR_FLAGS)
    try:
        nested.rename(outside / "reports")
        nested.symlink_to(outside / "reports", target_is_directory=True)
        with pytest.raises(OSError):
            nl_computer._assert_fd_path(
                nested_fd, nested, "整理目录", root_fd=root_fd, root=root)
    finally:
        os.close(nested_fd)
        os.close(root_fd)


async def test_organize_preview_fingerprint_detects_source_replacement(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-race") / "reports"
    target.mkdir(parents=True)
    source = target / "report.pdf"
    source.write_bytes(b"new")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    preview = await nl_computer.preview_organize_folder(target, "company-race")
    assert preview["ok"] is True, preview

    source.unlink()
    source.symlink_to(outside)
    result = await nl_computer.execute_organize_preview(
        target, preview_token=preview["preview_token"],
        expected_preview_hash=preview["preview_hash"], expected_path=preview["path"],
        company_id="company-race")

    assert result["ok"] is False and result["denied"] is True, result
    assert source.is_symlink()
    assert outside.read_bytes() == b"outside"
    assert not (target / "文档").exists()


def test_empty_paths_never_default_to_desktop():
    from app.core.sandbox import SandboxViolation

    for raw in ("", "   "):
        assert nl_computer.detect_intent(raw) is None
        try:
            nl_computer.resolve_organize_target(raw, "company-a")
        except SandboxViolation:
            pass
        else:
            raise AssertionError("空路径不得默认为桌面")


async def test_explicit_desktop_returns_preview_without_moving(
        allow_capability, monkeypatch, tmp_path):
    """显式桌面可以被识别，但第一轮只预览；沙箱内示范不碰真实桌面。"""
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    desktop = nl_computer.company_storage_root("company-f10") / "Desktop"
    desktop.mkdir(parents=True)
    (desktop / "report.pdf").write_bytes(b"x")

    intent = nl_computer.detect_intent("整理桌面文件")
    assert intent and intent["action"] == "organize_folder"
    assert intent["params"]["path"] == "Desktop", "识别层不得展开宿主桌面"
    result = await nl_computer.execute_nl(intent, "company-f10")
    assert result["ok"] is True and result["status"] == "preview", result
    assert result["path"] == str(desktop.resolve())
    assert (desktop / "report.pdf").is_file()
    assert not (desktop / "文档" / "report.pdf").exists()


async def test_chat_preview_confirmation_is_durable_and_one_time(
        client, make_user, make_company, monkeypatch, tmp_path):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    company_id = company["id"]
    work = nl_computer.company_storage_root(company_id) / "reports"
    work.mkdir(parents=True)
    (work / "report.pdf").write_bytes(b"x")

    async def allow(*args, **kwargs):
        return None

    monkeypatch.setattr(nl_computer, "_require_computer_permission", allow)

    first = await _stream(client, user, agent["id"], f"整理 {work} 文件夹")
    action = first["actions"][0]
    assert action["type"] == "nl_action"
    assert action["result"].get("status") == "preview", action["result"]
    assert action["result"]["awaiting_confirmation"] is True
    assert (work / "report.pdf").is_file(), "第一轮不得移动"

    history = await client.get(f"/agents/{agent['id']}/chat/history",
                               headers=user["headers"])
    preview_meta = history.json()["messages"][-1]["meta"]["organize_preview"]
    assert preview_meta["status"] == "pending"
    assert preview_meta["path"] == str(work.resolve())
    assert preview_meta["user_id"] == user["id"]
    assert preview_meta["preview_token"] == action["result"]["preview_token"]

    second = await _stream(client, user, agent["id"], "确认整理")
    assert second["actions"][0]["result"]["status"] == "completed"
    assert (work / "文档" / "report.pdf").is_file()

    third = await _stream(client, user, agent["id"], "确认整理")
    assert third["actions"][0]["result"]["ok"] is False, "预览只能消费一次"


async def test_concurrent_chat_confirmations_execute_preview_only_once(
        client, make_user, make_company, monkeypatch, tmp_path):
    from app.api.routes import agent_chat
    from app.db.session import AsyncSessionLocal
    from app.models.agent import Agent

    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    work = nl_computer.company_storage_root(company["id"]) / "reports"
    work.mkdir(parents=True)
    (work / "report.pdf").write_bytes(b"x")

    async def allow(*args, **kwargs):
        return None

    monkeypatch.setattr(nl_computer, "_require_computer_permission", allow)
    first = await _stream(client, user, agent["id"], f"整理 {work} 文件夹")
    assert first["actions"][0]["result"]["status"] == "preview"

    both_read_pending = asyncio.Barrier(2)
    pending = agent_chat._pending_organize_preview

    async def read_pending_together(*args, **kwargs):
        result = await pending(*args, **kwargs)
        await both_read_pending.wait()
        return result

    executions = []

    async def execute_once(*args, **kwargs):
        executions.append(kwargs["preview_token"])
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(agent_chat, "_pending_organize_preview", read_pending_together)
    monkeypatch.setattr(nl_computer, "execute_organize_preview", execute_once)

    async def confirm():
        async with AsyncSessionLocal() as db:
            row = await db.get(Agent, agent["id"])
            return await agent_chat._try_short_intents(
                db, row, user["id"], "确认整理")

    results = await asyncio.gather(confirm(), confirm())
    assert sorted(item["meta"]["confirmed"] for item in results) == [False, True]
    assert len(executions) == 1


async def test_chat_different_path_creates_new_preview_instead_of_reusing_old_one(
        client, make_user, make_company, monkeypatch, tmp_path):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    company_id = company["id"]
    work_a = nl_computer.company_storage_root(company_id) / "a"
    work_b = nl_computer.company_storage_root(company_id) / "b"
    for work in (work_a, work_b):
        work.mkdir(parents=True)
        (work / "report.pdf").write_bytes(b"x")

    async def allow(*args, **kwargs):
        return None

    monkeypatch.setattr(nl_computer, "_require_computer_permission", allow)

    first = await _stream(client, user, agent["id"], f"整理 {work_a} 文件夹")
    assert first["actions"][0]["result"].get("status") == "preview", first["actions"][0]["result"]
    reassigned = await _stream(client, user, agent["id"], f"整理 {work_b} 文件夹")
    assert reassigned["actions"][0]["result"]["status"] == "preview"
    assert reassigned["actions"][0]["result"]["path"] == str(work_b.resolve())
    assert (work_a / "report.pdf").is_file()
    assert (work_b / "report.pdf").is_file()

    confirmed = await _stream(client, user, agent["id"], "确认整理")
    assert confirmed["actions"][0]["result"]["status"] == "completed"
    assert (work_a / "report.pdf").is_file(), "旧路径预览不得被执行"
    assert (work_b / "文档" / "report.pdf").is_file()


async def test_business_task_intent_is_evaluated_before_computer_intent(
        client, make_user, make_company, monkeypatch):
    user = await make_user()
    company = await make_company(user)
    agent = await _make_agent(client, user, company)

    async def must_not_execute(*args, **kwargs):
        raise AssertionError("安排任务不该触发本机文件操作")
        yield  # pragma: no cover

    monkeypatch.setattr(nl_computer, "execute_nl", must_not_execute)
    done = await _stream(client, user, agent["id"], "安排任务：整理桌面文件")
    assert done["actions"][0]["type"] == "create_task"
    tasks = await client.get(f"/tasks?company_id={company['id']}",
                             headers=user["headers"])
    assert any(t["id"] == done["actions"][0]["task_id"] for t in tasks.json())


@pytest.mark.parametrize("windows", [False, True])
def test_organize_move_dispatches_to_native_platform_backend(monkeypatch, tmp_path, windows):
    calls = []
    monkeypatch.setattr(nl_computer, "_WINDOWS", windows)
    monkeypatch.setattr(
        nl_computer, "_move_organize_files_windows",
        lambda *args: calls.append(("windows", args)),
    )
    monkeypatch.setattr(
        nl_computer, "_move_organize_files_posix",
        lambda *args: calls.append(("posix", args)),
    )

    nl_computer._move_organize_files(tmp_path, [], [], "company-a")

    assert len(calls) == 1
    assert calls[0][0] == ("windows" if windows else "posix")
    assert calls[0][1] == (tmp_path, [], [], "company-a")


def test_windows_native_paths_use_extended_length_prefix(tmp_path):
    target = tmp_path / "文档" / "report.pdf"
    assert nl_computer._windows_nt_path(target) == f"\\\\?\\{target.absolute()}"


def test_windows_rename_payload_binds_verified_directory_and_filename():
    import ctypes
    from ctypes import wintypes

    name = "季度报告.pdf"
    root_directory = 1234
    buffer, size = nl_computer._windows_rename_buffer(name, root_directory)

    class RenameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_byte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    payload = RenameInformation.from_buffer_copy(buffer.raw[:ctypes.sizeof(RenameInformation)])
    filename = ctypes.string_at(
        ctypes.addressof(buffer) + RenameInformation.FileName.offset,
        payload.FileNameLength,
    ).decode("utf-16-le")

    assert size == len(buffer.raw)
    assert payload.ReplaceIfExists == 0
    root_handle = payload.RootDirectory.value if hasattr(payload.RootDirectory, "value") \
        else payload.RootDirectory
    assert root_handle == root_directory
    assert payload.FileNameLength == len(name.encode("utf-16-le"))
    assert filename == name


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")),
                    reason="POSIX 原子禁止覆盖移动只在 macOS/Linux 验证")
def test_posix_rename_noreplace_is_atomic_and_preserves_existing_target(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"source")
    source_fd = os.open(source_dir, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        nl_computer._rename_noreplace(source_fd, source.name, target_fd, source.name)
        assert not source.exists()
        assert (target_dir / source.name).read_bytes() == b"source"
        (target_dir / source.name).write_bytes(b"existing")
        source.write_bytes(b"replacement")

        with pytest.raises(FileExistsError):
            nl_computer._rename_noreplace(source_fd, source.name, target_fd, source.name)

        assert source.read_bytes() == b"replacement"
        assert (target_dir / source.name).read_bytes() == b"existing"
    finally:
        os.close(source_fd)
        os.close(target_fd)


async def test_organize_cleanup_recovers_from_open_file_after_last_backup_removed(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-cleanup-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"report")
    real_unlink = os.unlink
    injected = False

    def remove_destination_before_backup(path, *args, **kwargs):
        nonlocal injected
        if (not injected and isinstance(path, str)
                and path.startswith(".aieos-organize-")):
            injected = True
            (target / "文档" / "report.pdf").unlink()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(nl_computer.os, "unlink", remove_destination_before_backup)
    with pytest.raises(OSError):
        await nl_computer.organize_folder(target, company_id="company-cleanup-race")

    assert injected
    _safe_backup(target.parent, b"report")
    assert not (target / "文档" / "report.pdf").exists()


async def test_organize_rejects_reopened_directory_with_different_identity(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-fd-race") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"report")
    decoy = target.parent / "decoy"
    decoy.mkdir()
    real_open_relative = nl_computer._open_relative_dir
    injected = False

    def open_decoy(root_fd, root, path):
        nonlocal injected
        if not injected and path == target / "文档" and (target / "文档").is_dir():
            injected = True
            return os.open(decoy, nl_computer._DIR_FLAGS)
        return real_open_relative(root_fd, root, path)

    monkeypatch.setattr(nl_computer, "_open_relative_dir", open_decoy)
    with pytest.raises(OSError, match="移出公司沙箱"):
        await nl_computer.organize_folder(target, company_id="company-fd-race")

    assert injected
    _safe_backup(target.parent, b"report")
    assert not (decoy / "report.pdf").exists()


async def test_organize_cleanup_unlink_error_after_deletion_recovers_from_fd(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-unlink-error") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"report")
    real_unlink = os.unlink
    injected = False

    def unlink_then_fail(path, *args, **kwargs):
        nonlocal injected
        result = real_unlink(path, *args, **kwargs)
        if (not injected and isinstance(path, str)
                and path.startswith(".aieos-organize-")):
            injected = True
            (target / "文档" / "report.pdf").unlink()
            raise OSError(errno.EIO, "injected after unlink")
        return result

    monkeypatch.setattr(nl_computer.os, "unlink", unlink_then_fail)
    with pytest.raises(OSError):
        await nl_computer.organize_folder(target, company_id="company-unlink-error")

    assert injected
    _safe_backup(target.parent, b"report")


async def test_organize_cleanup_never_recovers_corrupted_destination_as_safe(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-corrupt-cleanup") / "reports"
    target.mkdir(parents=True)
    (target / "report.pdf").write_bytes(b"report")
    real_unlink = os.unlink
    injected = False

    def corrupt_after_unlink(path, *args, **kwargs):
        nonlocal injected
        result = real_unlink(path, *args, **kwargs)
        if (not injected and isinstance(path, str)
                and path.startswith(".aieos-organize-")):
            injected = True
            (target / "文档" / "report.pdf").write_bytes(b"CORRUPTED")
        return result

    monkeypatch.setattr(nl_computer.os, "unlink", corrupt_after_unlink)
    with pytest.raises(OSError, match="恢复也失败.*report.pdf"):
        await nl_computer.organize_folder(target, company_id="company-corrupt-cleanup")

    assert injected
    assert not any(path.read_bytes() == b"CORRUPTED"
                   for path in target.parent.glob(".aieos-organize-*") if path.is_file())


async def test_organize_multi_file_rollback_reports_every_unrecovered_file(
        allow_capability, monkeypatch, tmp_path):
    monkeypatch.setattr(nl_computer, "storage_root", lambda: tmp_path.resolve())
    target = nl_computer.company_storage_root("company-multi-error") / "reports"
    target.mkdir(parents=True)
    (target / "a.pdf").write_bytes(b"a")
    (target / "b.pdf").write_bytes(b"bb")
    real_unlink = os.unlink
    real_recover_fd = nl_computer._recover_organize_fd
    backup_unlinks = 0

    def fail_larger_recovery(root_fd, source_fd, expected):
        if expected[2] == 2:
            raise OSError(errno.EIO, "injected fd recovery failure")
        return real_recover_fd(root_fd, source_fd, expected)

    monkeypatch.setattr(nl_computer, "_recover_organize_fd", fail_larger_recovery)

    def fail_second_cleanup(path, *args, **kwargs):
        nonlocal backup_unlinks
        if isinstance(path, str) and path.startswith(".aieos-organize-"):
            backup_unlinks += 1
            if backup_unlinks == 2:
                (target / "文档" / "b.pdf").unlink()
                real_unlink(path, *args, **kwargs)
                raise OSError(errno.EIO, "injected second cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(nl_computer.os, "unlink", fail_second_cleanup)
    with pytest.raises(OSError, match="恢复也失败.*b.pdf"):
        await nl_computer.organize_folder(target, company_id="company-multi-error")

    _safe_backup(target.parent, b"a")
    assert backup_unlinks == 2
