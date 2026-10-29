"""Computer Agent 权限门禁回归测试（P0-4）。

钉死三类曾真实存在的绕过：

1. `type_text` / `click` / `key` 不在 HIGH_RISK 集合里——先
   `open -a Terminal` 再 type_text，就等价于主机命令执行，
   完全绕开 `core/sandbox.py` 的 shell 白名单；
2. 自然语言入口 `execute_nl()` 不校验租户与计算机能力授权，
   任何一条聊天消息都能触发主机操作；
3. 文件整理可以作用于沙箱之外的任意目录。

测试纪律：所有真实执行（osascript / open / 子进程 / 文件移动）
全部 mock 或限制在临时 storage 内，不触发任何本机 GUI 操作，
也不碰生产数据库（conftest 使用临时 SQLite）。
"""
from __future__ import annotations

import shutil

import pytest

import app.computer_real as computer_real
import app.nl_computer as nl_computer
from app.api.routes.computer_agent import HIGH_RISK_CAPABILITIES


# ============================================================ 高危能力集合

def test_high_risk_set_covers_gui_input_capabilities():
    """行级回归：GUI 注入能力与 shell 同级，必须走审批门。"""
    for cap in ("type_text", "click", "key"):
        assert cap in HIGH_RISK_CAPABILITIES, (
            f"{cap} 不在 HIGH_RISK_CAPABILITIES 中——"
            "可绕过审批直接向前台应用注入键鼠事件")


# ============================================================ REST 审批门

async def test_type_text_held_for_approval_even_when_granted(client, tenants):
    """已授权 type_text + 公司开启 computer_command 审批 → 只建单不执行。"""
    co = tenants.alice_co["id"]
    headers = tenants.alice["headers"]

    r = await client.post("/computer-agent/grant",
                          params={"company_id": co, "capability": "type_text"},
                          headers=headers)
    assert r.status_code == 200 and r.json()["granted"] is True

    r = await client.put("/intervention/policy",
                         params={"company_id": co},
                         json={"level": 2,
                               "approval_actions": ["computer_command"]},
                         headers=headers)
    assert r.status_code == 200

    r = await client.post("/computer-agent/execute",
                          params={"company_id": co},
                          json={"action_type": "type_text",
                                "params": {"text": "rm -rf ~/Documents"}},
                          headers=headers)
    body = r.json()

    # 现有语义：需要审批时返回 awaiting_approval（HOLD），
    # 若未来改成 403/409 也算正确——关键是绝不能真的执行。
    assert r.status_code in (200, 403, 409), \
        f"未知的审批门语义：{r.status_code} {body}"
    assert body.get("status") == "awaiting_approval" or r.status_code in (403, 409)
    assert body.get("result") is None, "审批前竟产生了执行结果"
    assert "log_id" not in body, "审批前不应出现执行日志"

    # 审批单必须真的落库，否则「等老板批准」只是一句提示
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.intervention import ApprovalRequest

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ApprovalRequest).where(
            ApprovalRequest.company_id == co))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action_type == "computer_command"
    assert rows[0].payload["capability"] == "type_text"
    assert rows[0].status == "PENDING"


# ============================================================ execute_nl 签名

async def test_execute_nl_requires_company_id():
    """company_id 必填：没有租户上下文就不执行，不能靠默认值兜底。"""
    with pytest.raises(TypeError):
        await nl_computer.execute_nl(
            {"action": "open_app", "params": {"app": "Calculator"}})


# ============================================================ 文件沙箱

@pytest.fixture
def allow_capability(monkeypatch):
    """跳过 DB 权限查询，把用例聚焦在路径/宿主校验上。"""

    async def _allow(*args, **kwargs):
        return None

    monkeypatch.setattr(nl_computer, "_require_computer_permission", _allow)


async def test_execute_nl_organize_outside_storage_rejected(
        allow_capability, monkeypatch):
    """沙箱外目录（/etc）必须被拒绝，且不得触碰文件系统。"""
    async def _no_subprocess(*args, **kwargs):  # pragma: no cover
        raise AssertionError("不应启动任何子进程")

    monkeypatch.setattr(nl_computer.asyncio, "create_subprocess_exec",
                        _no_subprocess)

    result = await nl_computer.execute_nl(
        {"action": "organize_folder", "params": {"path": "/etc"}},
        "company-gate-test")

    assert result["ok"] is False
    assert result.get("denied") is True
    assert "沙箱" in (result.get("error") or "")


async def test_execute_nl_organize_rejects_path_traversal(allow_capability):
    """`storage/../../` 这类上跳必须在 resolve() 后被拦住。"""
    evil = nl_computer.storage_root() / ".." / ".." / "escaped_by_nl"
    result = await nl_computer.execute_nl(
        {"action": "organize_folder", "params": {"path": str(evil)}},
        "company-gate-test")

    assert result["ok"] is False
    assert result.get("denied") is True


async def test_execute_nl_organize_inside_storage_still_works(allow_capability):
    """反向对照：沙箱内目录必须能整理，否则等于把功能锁死。"""
    work = nl_computer.storage_root() / "gate_organize_probe"
    work.mkdir(parents=True, exist_ok=True)
    (work / "probe.png").write_bytes(b"x")
    try:
        result = await nl_computer.execute_nl(
            {"action": "organize_folder", "params": {"path": str(work)}},
            "company-gate-test")
        assert result["ok"] is True, result
        assert (work / "图片" / "probe.png").is_file()
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ============================================================ shell 宿主

async def test_execute_nl_open_app_rejects_shell_host(
        allow_capability, monkeypatch):
    """Terminal 是 shell 宿主，自然语言入口必须明确拒绝。"""
    async def _no_subprocess(*args, **kwargs):  # pragma: no cover
        raise AssertionError("终端类应用不应被启动")

    monkeypatch.setattr(nl_computer.asyncio, "create_subprocess_exec",
                        _no_subprocess)

    result = await nl_computer.execute_nl(
        {"action": "open_app", "params": {"app": "Terminal"}},
        "company-gate-test")

    assert result["ok"] is False
    assert result.get("denied") is True
    assert "终端" in result["message"]


# ============================================================ type_text 前台检查

async def test_real_type_text_blocks_terminal_frontmost(monkeypatch):
    """前台是 Terminal → 拒绝 keystroke，绝不能发出按键。"""
    async def fake_run(script: str):
        if "frontmost" in script:
            return True, "Terminal"
        raise AssertionError("前台为终端时不应发送 keystroke")

    monkeypatch.setattr(computer_real, "_run_osascript", fake_run)

    result = await computer_real.real_type_text({"text": "whoami"})
    assert result["ok"] is False
    assert result.get("blocked") is True
    assert result["frontmost_app"] == "Terminal"


async def test_real_type_text_allows_when_frontmost_unknown(monkeypatch):
    """取不到前台应用时按允许处理（只记 warning），不把功能改死。"""
    calls: list[str] = []

    async def fake_run(script: str):
        calls.append(script)
        if "frontmost" in script:
            return False, "System Events got an error"
        return True, ""

    monkeypatch.setattr(computer_real, "_run_osascript", fake_run)

    result = await computer_real.real_type_text({"text": "hello"})
    assert result["ok"] is True
    assert len(calls) == 2, "前台查询失败后仍应继续执行 keystroke"


async def test_real_type_text_allows_normal_frontmost(monkeypatch):
    """前台是普通应用时正常键入（反向对照）。"""
    async def fake_run(script: str):
        if "frontmost" in script:
            return True, "Notes"
        return True, ""

    monkeypatch.setattr(computer_real, "_run_osascript", fake_run)

    result = await computer_real.real_type_text(
        {"text": "hello"}, app_hint="Notes")
    assert result["ok"] is True
    assert result["frontmost_app"] == "Notes"
