"""无障碍树（AX）执行器与「不依赖截图」这条桌面操作链路的回归测试。

## 测试纪律

本模块的用例**一次都不真的调用 osascript**：`_osascript` 全部换成假实现，
返回事先准备好的 AppleScript 输出文本。在这台机器上真的去点按钮、
往输入框赋值是不可接受的副作用，即使目标是"安全"的应用也一样。
覆盖三件事：

1. 引用（ref）的构建/解析与越界拒绝——点击与赋值全靠它定位元素；
2. AppleScript 脚本渲染与快照解析——参数拼错、字段错位都会在真机上
   表现成"点错元素"；
3. 三档策略与审批门对能力的分类：只读动作直接放行、界面写动作要审批、
   终端宿主的赋值硬拦截。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import app.computer_ax as ax
from app.computer_agent import ComputerPermission
from app.db.session import AsyncSessionLocal
from app.models.intervention import ApprovalRequest
from app.models.plugin import ComputerPolicy
from app.services import computer_policy as policy
from app.services import computer_service


def _ref(app: str = "Safari", window: int = 1, path: list[int] | None = None) -> str:
    return ax.build_ref(app, window, path or [3, 1])


# ============================================================ 元素引用


def test_build_ref_roundtrip_with_special_app_names():
    """应用名里的 `|`、空格、中文都不能把引用拆坏。"""
    for name in ("Safari", "微信 3.x", "My|App", "Visual Studio Code"):
        app, window, path = ax.parse_ref(_ref(name, 2, [1, 2, 3]))
        assert (app, window, path) == (name, 2, [1, 2, 3])


@pytest.mark.parametrize("bad", [
    "",                                  # 空
    "Safari|1",                          # 缺层级
    "Safari|1|0",                        # 下标从 1 起
    "Safari|0|1",                        # 窗口序号越界
    "Safari|99|1",
    "Safari|x|1",                        # 窗口非整数
    "Safari|1|a",                        # 层级非整数
    "Safari|1|" + ".".join(["1"] * 7),   # 层级过深
    "|".join([""] * 3),                  # 空应用名
])
def test_parse_ref_rejects_malformed(bad):
    with pytest.raises(ax.AXError):
        ax.parse_ref(bad)


def test_element_expr_nests_ui_elements_and_quotes_app():
    expr = ax.element_expr('Bad"App', 2, [1, 3])
    assert expr == (
        'item 3 of (UI elements of item 1 of (UI elements of '
        'window 2 of process "Bad\\"App"))')


def test_applescript_text_escapes_quotes_and_control_chars():
    text = ax._applescript_text('a"b\\c\nd\te')
    assert '\\"' in text and '\\\\' in text
    assert text.endswith('& return & "d" & tab & "e"')


# ============================================================ 脚本渲染与解析


def test_render_dump_has_no_leftover_tokens():
    script = ax._render_dump('"Finder"', 2, 40, 3)
    for token in ("__APP__", "__WIN__", "__MAX_NODES__", "__MAX_DEPTH__"):
        assert token not in script
    assert 'window 2 of process "Finder"' in script
    assert "set gMaxNodes to 40" in script and "set gMaxDepth to 3" in script
    # AppleScript 里 `global` 必须写在 handler 之前，否则运行时"变量未定义"
    assert script.index("global gLines") < script.index("on walk")


def test_parse_dump_maps_fields_and_builds_refs():
    raw = "\t".join(["1.2", "AXButton", "100", "200", "40", "20", "确定"])
    rows = ax._parse_dump(raw + "\n\n", "Safari", 1)
    assert rows == [{
        "ref": _ref("Safari", 1, [1, 2]), "path": "1.2", "role": "AXButton",
        "title": "确定", "x": 100, "y": 200, "width": 40, "height": 20,
        "center": [120, 210]}]


def test_parse_dump_keeps_tabs_in_title_and_survives_missing_geometry():
    """标题里可能有制表符；几何取不到时不能整行丢掉，也不能填假坐标。"""
    raw = "\t".join(["1", "AXStaticText", "-1", "-1", "-1", "-1", "a\tb"])
    rows = ax._parse_dump(raw, "Finder", 1)
    assert rows[0]["title"] == "a\tb"
    assert "x" not in rows[0] and "center" not in rows[0]


@pytest.mark.parametrize("line", [
    "AXButton\t10\t20",      # 字段不足
    "x.1\tAXButton\t1\t2\t3\t4\t标题",   # 层级不是整数
    "1\tAXButton\t1\t2\t3\t4",           # 缺标题列
])
def test_parse_dump_skips_unusable_lines(line):
    assert ax._parse_dump(line, "Finder", 1) == []


@pytest.mark.parametrize("bad", ["", "   ", "x" * 100, "带\x07响铃"])
def test_clean_app_name_rejects_empty_oversize_and_control_chars(bad):
    with pytest.raises(ax.AXError):
        ax._clean_app_name(bad)


def test_int_param_clamps_and_rejects_non_integer():
    assert ax._int_param({"window": 999}, "window", 1, lo=1, hi=50) == 50
    assert ax._int_param({}, "window", 1, lo=1, hi=50) == 1
    assert ax._int_param({"window": None}, "window", 1, lo=1, hi=50) == 1
    with pytest.raises(ax.AXError):
        ax._int_param({"window": "abc"}, "window", 1, lo=1, hi=50)


def test_pair_parses_position_and_size():
    assert ax._pair("10,20") == [10, 20]
    assert ax._pair("-1,-1") == [-1, -1]
    assert ax._pair("12.7,3") == [12, 3]
    assert ax._pair("abc") == []


def test_permission_hint_maps_real_error_codes():
    assert "辅助功能" in ax._permission_hint(
        "osascript: error -1719: 不允许辅助访问")
    assert "list_windows" in ax._permission_hint(
        "error: -1728 无法获取对象")
    assert ax._permission_hint("something else") == "something else"


# ============================================================ 执行器（全部假 osascript）


@pytest.fixture
def fake_osascript(monkeypatch):
    """替换掉唯一的系统调用出口，记录脚本内容并回放预设输出。"""
    calls: list[str] = []
    state = {"output": "", "ok": True, "err": ""}

    async def _fake(script: str, timeout: int = 20):
        calls.append(script)
        return state["ok"], state["output"], state["err"]

    monkeypatch.setattr(ax, "_osascript", _fake)
    return SimpleNamespace(calls=calls, state=state)


async def test_list_windows_groups_by_process(fake_osascript):
    fake_osascript.state["output"] = (
        "Finder\t1\t100,200\t1200,800\t桌面\n"
        "Safari\t1\t0,25\t1440,900\tApple\n"
        "Safari\t2\t10,10\t800,600\t新标签页\n"
        "Safari\t3\tbad\tparse\t无坐标\n"
        "Notes\t0\n")
    out = await ax.ax_list_windows({})
    assert out["ok"] and out["app_count"] == 3
    saf = [a for a in out["apps"] if a["app"] == "Safari"][0]
    assert [w["window"] for w in saf["windows"]] == [1, 2, 3]
    assert saf["windows"][0]["position"] == [0, 25]
    # 坐标读不到的窗口照样列出来——宁可少给信息，也不能让 Agent 以为窗口不存在
    assert saf["windows"][2]["position"] == [] and saf["windows"][2]["size"] == []
    notes = [a for a in out["apps"] if a["app"] == "Notes"][0]
    assert notes["windows"] == []


async def test_list_windows_failure_is_readable_not_raised(fake_osascript):
    fake_osascript.state.update({"ok": False, "err": "error -1719"})
    out = await ax.ax_list_windows({})
    assert out["ok"] is False and "辅助功能" in out["error"]


async def test_snapshot_uses_frontmost_app_and_reports_truncation(fake_osascript):
    fake_osascript.state["output"] = "Notes\n" + "\n".join(
        "\t".join([str(i), "AXButton", "0", "0", "10", "10", f"b{i}"])
        for i in range(1, 12))
    out = await ax.ax_accessibility_snapshot({"window": 1})
    assert out["app"] == "Notes"
    assert out["element_count"] == 11
    # 默认上限 80，这里远没到，不该谎报截断
    assert out["truncated"] is False
    assert "process \"Notes\"" in fake_osascript.calls[-1]


async def test_find_element_matches_title_and_filters_by_role(fake_osascript):
    fake_osascript.state["output"] = "\n".join([
        "\t".join(["1", "AXButton", "0", "0", "10", "10", "发送"]),
        "\t".join(["2", "AXStaticText", "0", "0", "10", "10", "发送中"]),
        "\t".join(["3", "AXTextField", "0", "0", "10", "10", "收件人"]),
    ])
    hit = await ax.ax_find_element({"app": "Mail", "query": "发送"})
    assert hit["match_count"] == 2
    strict = await ax.ax_find_element({"app": "Mail", "query": "发送",
                                       "role": "AXButton"})
    assert strict["match_count"] == 1 and strict["matches"][0]["role"] == "AXButton"
    assert strict["matches"][0]["ref"] == _ref("Mail", 1, [1])
    # 空查询由预检拦下，执行器自己也绝不扫全树
    with pytest.raises(ax.AXError):
        await ax.ax_find_element({"app": "Mail", "query": "  "})


async def test_get_window_reports_geometry_and_focus(fake_osascript):
    fake_osascript.state["output"] = "报告.pdf\t50,60\t900,700\t26\tAXTextArea"
    out = await ax.ax_get_window({"app": "预览", "window": 2})
    assert out["title"] == "报告.pdf"
    assert out["position"] == [50, 60] and out["size"] == [900, 700]
    assert out["top_level_count"] == 26 and out["focused_role"] == "AXTextArea"


async def test_click_element_whitelists_actions_and_passes_hint_on_failure(
        fake_osascript):
    with pytest.raises(ax.AXError):
        await ax.ax_click_element({"ref": _ref(), "action": "AXShowMenu"})
    fake_osascript.state["ok"] = True
    out = await ax.ax_click_element({"ref": _ref("Finder", 1, [2])})
    assert out["ok"] and out["performed"] is True
    assert 'perform action "AXPress"' in fake_osascript.calls[-1]
    fake_osascript.state.update({"ok": False, "err": "error -1728"})
    failed = await ax.ax_click_element({"ref": _ref()})
    assert failed["ok"] is False and "重新取一次快照" in failed["hint"]


async def test_set_value_blocks_terminal_hosts(fake_osascript):
    """往终端界面赋值 == 提交命令，执行器和策略两层都得拦。"""
    out = await ax.ax_set_value({"ref": _ref("Terminal"), "value": "rm -rf ~"})
    assert out["ok"] is False and out["blocked"] is True
    assert "run_shell" in out["error"]
    assert fake_osascript.calls == []


@pytest.mark.parametrize("value", ["", "a" * 2001, "带\x07响铃"])
async def test_set_value_rejects_bad_payloads(value, fake_osascript):
    with pytest.raises(ax.AXError):
        await ax.ax_set_value({"ref": _ref(), "value": value})


async def test_set_value_writes_by_ax_value(fake_osascript):
    out = await ax.ax_set_value({"ref": _ref("Safari", 1, [4]),
                                 "value": "https://example.com"})
    assert out["ok"] and out["method"] == "AXValue" and out["chars"] == 19
    script = fake_osascript.calls[-1]
    assert "set value of" in script and '"https://example.com"' in script


async def test_non_macos_refuses_instead_of_mysterious_failure(monkeypatch):
    monkeypatch.setattr(ax.platform, "system", lambda: "Linux")
    with pytest.raises(ax.AXError, match="agent-desktop"):
        await ax._osascript("whatever")


# ============================================================ 能力清单与预检


def test_ax_capabilities_are_registered_and_classified():
    from app.computer_real import EXTENDED_CAPABILITIES, REAL_EXECUTORS

    for cap in ax.AX_EXECUTORS:
        assert cap in EXTENDED_CAPABILITIES
        assert REAL_EXECUTORS[cap] is ax.AX_EXECUTORS[cap]
    # 只读的四个动作必须归进 OBSERVE，否则默认模式下每次快照都要审批
    for cap in ("list_windows", "accessibility_snapshot", "get_window",
                "find_element"):
        assert cap in policy.OBSERVE_ACTIONS
    for cap in ("click_element", "set_value"):
        assert cap not in policy.OBSERVE_ACTIONS


async def test_precheck_rejects_missing_params_before_applescript():
    from app.services.computer_service import _precheck_capability

    assert _precheck_capability("list_windows", {}) is None
    for cap, params in [("find_element", {}), ("click_element", {"ref": ""}),
                        ("set_value", {}), ("get_window", {})]:
        blocked = _precheck_capability(cap, params)
        assert blocked and blocked["ok"] is False and blocked["precheck"]


# ============================================================ 三档策略分类


def _policy_row(mode: str, **kw) -> ComputerPolicy:
    return ComputerPolicy(company_id="co", mode=mode,
                          auto_dirs=kw.get("auto_dirs", []),
                          auto_commands=kw.get("auto_commands", []),
                          auto_apps=kw.get("auto_apps", []),
                          notify_on_finish=True)


@pytest.mark.parametrize("cap", ["accessibility_snapshot", "list_windows",
                                 "get_window", "find_element"])
def test_observe_caps_run_without_approval(cap):
    v = policy.evaluate_action(_policy_row("default"), cap, {"app": "Finder"})
    assert v["decision"] == "allow" and v["matched"] == "observe"


@pytest.mark.parametrize("cap", ["click_element", "set_value"])
def test_ui_write_caps_need_approval_in_default_mode(cap):
    assert policy.evaluate_action(
        _policy_row("default"), cap, {"ref": _ref("Safari")})["decision"] == "approval"


def test_ui_write_caps_follow_app_whitelist_in_auto_mode():
    row = _policy_row("auto", auto_apps=["Safari"])
    allowed = policy.evaluate_action(row, "click_element", {"ref": _ref("Safari")})
    assert allowed["decision"] == "allow" and "应用白名单" in allowed["reason"]
    held = policy.evaluate_action(row, "click_element", {"ref": _ref("Notes")})
    assert held["decision"] == "approval"


def test_set_value_into_terminal_ref_is_hard_blocked():
    """策略层的红线用子串匹配，`iTerm` 这种带后缀的进程名也拦得住。"""
    for name in ("Terminal", "iTerm", "iTerm 2", "Warp"):
        verdict = policy.evaluate_action(None, "set_value",
                                         {"ref": _ref(name), "value": "ls"})
        assert verdict["decision"] == "blocked", name
    # 编码过的应用名不能靠转义绕过
    verdict = policy.evaluate_action(None, "set_value",
                                     {"ref": quote("Terminal") + "|1|2",
                                      "value": "ls"})
    assert verdict["decision"] == "blocked"


def test_hard_block_reason_is_callable_without_a_policy_row():
    assert policy.hard_block_reason(
        "set_value", {"ref": _ref("Terminal")})
    assert policy.hard_block_reason(
        "accessibility_snapshot", {"app": "Finder"}) is None


# ============================================================ 聊天入口共用审批门


async def test_chat_computer_tool_holds_for_approval(client, tenants, monkeypatch):
    """聊天里的 computer_* 必须和 REST 走同一个门：默认审批模式下一律不落执行器。

    改造前聊天入口只查授权、不查策略，「老板在设置页开了默认审批」对
    模型在对话里的操作没有任何约束。
    """
    from app.api.routes import agent_chat

    cid = tenants.alice_co["id"]
    executed: list[str] = []

    async def _fake_executor(params: dict) -> dict:
        executed.append("ran")
        return {"ok": True, "performed": True}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "click_element",
                        _fake_executor)
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability="click_element", granted=True))
        s.add(ComputerPolicy(company_id=cid, mode="default", auto_dirs=[],
                              auto_commands=[], auto_apps=[],
                              notify_on_finish=True))
        await s.commit()

        out = await agent_chat._computer_action(
            s, "click_element", {"ref": _ref("Safari")},
            tenants.alice["id"], cid)
        assert executed == [], "默认审批模式下聊天入口真的执行了界面写操作"
        assert out["ok"] is False and out["awaiting_approval"] is True
        assert out["approval_request_id"]

        req = await s.get(ApprovalRequest, out["approval_request_id"])
        assert req.action_type == "computer_command"
        assert req.payload["capability"] == "click_element"


async def test_chat_computer_tool_denies_ungranted_capability(client, tenants,
                                                              monkeypatch):
    """审批门不能替授权开门：没勾过的能力照样拒绝，且一条都不执行。"""
    from app.api.routes import agent_chat

    cid = tenants.alice_co["id"]
    ran: list = []
    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "accessibility_snapshot",
                        _recording_executor(ran))
    async with AsyncSessionLocal() as s:
        out = await agent_chat._computer_action(
            s, "accessibility_snapshot", {"app": "Finder"},
            tenants.alice["id"], cid)
    assert ran == []
    assert out["ok"] is False and out["denied"] is True
    assert "未获授权" in out["error"]


async def test_service_hard_blocks_even_after_grant(client, tenants, monkeypatch):
    """授权 + 审批都挡不住红线：执行服务自己必须再问一次。"""
    cid = tenants.alice_co["id"]
    ran: list = []
    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "set_value",
                        _recording_executor(ran))
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability="set_value", granted=True))
        await s.commit()

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="set_value",
            params={"ref": _ref("Terminal"), "value": "id"})
    assert ran == []
    assert outcome["ok"] is False and outcome["decision"] == "blocked"
    assert "终端" in outcome["deny_reason"]


def _recording_executor(sink: list):
    async def _run(params: dict) -> dict:
        sink.append(params)
        return {"ok": True}
    return _run
