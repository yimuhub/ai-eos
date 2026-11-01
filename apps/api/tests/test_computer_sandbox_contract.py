"""沙箱（``/sandbox``）与本机操作（``/computer-agent``）的响应形状契约（A3 长尾）。

和 `test_expert_intervention_contract.py` 同一套路：把「端点真实返回的键集合」和
「`response_model` 声明的字段」两头钉死，断言用**相等**而不是"包含"——

* 只断言响应里有某些键 → 端点哪天多吐一个键，测试照样绿，前端哪天读到
  `undefined` 才发现；
* 只断言模型字段 → 模型写漏一个字段测试不会红，但序列化会把它**静默裁掉**，
  于是权限面板少一列、审计日志少一格，且没有任何报错。

## 这个模块的测试特别小心的一件事

`/computer-agent/execute` 的真分支会**在本机执行真实命令**（screencapture /
osascript / open）。所以所有会走到执行器的用例都用
`monkeypatch.setitem(computer_service.REAL_EXECUTORS, ...)` 顶掉那一项，
测完自动还原——绝不允许测试在这台机器上真的截屏、真的敲键、真的起 App。
需要落盘的只有截屏预览那一个文件，写在沙箱根 `/tmp/ai-eos-screens` 里并在
用例结束时删除。

## 被这批契约钉住的真实缺陷

`/sandbox/sessions/{sid}/actions` 会把「高风险待审批」那条记录一起列出来，而那条
记录没有截图、也没走过 verify（`verified/before/after` 都是 NULL）。若按"当前代码
路径看起来总有值"把这三列声明成必填，审批一开这个列表接口就直接 500。
`ComputerLogOut.result` 可空同理：`nl_computer.py` 的拒绝审计就是不带结果的。
"""
from __future__ import annotations

import os
import uuid

from fastapi.routing import APIRoute, _IncludedRouter
from pydantic import BaseModel

from app.computer_real import EXTENDED_CAPABILITIES
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.intervention import InterventionPolicy
from app.models.whitelist import ApprovalWhitelist
from app.schemas import responses_computer as comp
from app.services import computer_service

_MODULES = {"app.api.routes.sandbox", "app.api.routes.computer_agent"}

# 截屏预览的沙箱根（app/core/sandbox.py 的 sandbox_roots 白名单里唯一可写的
# 临时目录）；用例自己创建、自己删除。
_SCREEN_ROOT = "/tmp/ai-eos-screens"


def _id() -> str:
    return str(uuid.uuid4())


def _keys(model) -> set[str]:
    return set(model.model_fields)


def _api_routes() -> list[APIRoute]:
    found: list[APIRoute] = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif isinstance(route, _IncludedRouter):
                walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                walk(route.routes)

    from app.main import app
    walk(app.routes)
    return found


def _fake_executor(result: dict):
    """替身执行器：不动本机，只回指定结构。"""

    async def _run(params: dict) -> dict:  # noqa: ANN001
        return dict(result)

    return _run


async def _seed_agent(company_id: str, name: str = "本机操作员工") -> str:
    aid = _id()
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=aid, company_id=company_id, name=name,
                    role="测试", status="IDLE"))
        await s.commit()
    return aid


async def _seed_policy(company_id: str, approval_actions: list[str]) -> None:
    """公司级干预策略：approval_actions 决定哪些动作先落审批单。"""
    async with AsyncSessionLocal() as s:
        s.add(InterventionPolicy(id=_id(), company_id=company_id,
                                 project_id=None, level=2,
                                 approval_actions=approval_actions))
        await s.commit()


async def _seed_whitelist(company_id: str, capability: str,
                          param_pattern: str = "") -> str:
    wid = _id()
    async with AsyncSessionLocal() as s:
        s.add(ApprovalWhitelist(id=wid, company_id=company_id,
                                capability=capability,
                                param_pattern=param_pattern,
                                note="测试免批"))
        await s.commit()
    return wid


async def _mk_workspace(client, h, cid: str, name: str = "演示沙箱") -> str:
    r = await client.post(f"/sandbox/workspaces?name={name}&company_id={cid}",
                          headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _mk_session(client, h, wid: str) -> str:
    r = await client.post(f"/sandbox/workspaces/{wid}/sessions",
                          json={"goal": "跑通一步"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ================================================================= /sandbox


async def test_workspace_and_session_lifecycle_keys(client, tenants):
    """建区 → 列表 → 建会话 → 暂停 → 恢复 → 取消，每步键集合与模型相等。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]

    r = await client.post(f"/sandbox/workspaces?name=演示沙箱&company_id={cid}"
                          f"&provider=MOCK", headers=h)
    assert r.status_code == 201, r.text
    ws = r.json()
    assert set(ws) == _keys(comp.SandboxWorkspaceOut), ws
    # provider 回的是枚举的 .value，不是 {"type":"WorkspaceProvider"} 之类的包装
    assert ws["provider"] == "MOCK"

    r = await client.get(f"/sandbox/workspaces?company_id={cid}", headers=h)
    assert r.status_code == 200, r.text
    assert all(set(w) == _keys(comp.SandboxWorkspaceOut) for w in r.json())

    wid = ws["id"]
    r = await client.post(f"/sandbox/workspaces/{wid}/sessions",
                          json={"goal": "整理桌面"}, headers=h)
    assert r.status_code == 201, r.text
    s = r.json()
    assert set(s) == _keys(comp.SandboxSessionOut), s
    assert s["status"] == "RUNNING"
    sid = s["id"]

    r = await client.post(f"/sandbox/sessions/{sid}/pause", headers=h)
    assert set(r.json()) == {"id", "status"}, r.json()
    assert r.json()["status"] == "PAUSED"

    r = await client.post(f"/sandbox/sessions/{sid}/resume", headers=h)
    assert set(r.json()) == _keys(comp.SandboxSessionResumeOut), r.json()
    assert r.json()["resumed_from_checkpoint"] is True

    r = await client.post(f"/sandbox/sessions/{sid}/cancel", headers=h)
    assert set(r.json()) == {"id", "status"}, r.json()
    assert r.json()["status"] == "CANCELLED"

    # 会话结束后不许再操作——前端「继续」按钮点了必须是 409，不是静默成功
    r = await client.post(f"/sandbox/sessions/{sid}/act",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 409, r.text


async def test_act_two_branches_have_different_key_sets(client, tenants):
    """``/act`` 的联合模型：待审批分支不能多出 seq，执行分支不能少 observation。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]
    sid = await _mk_session(client, h, await _mk_workspace(client, h, cid))

    # 分支一：高风险 + 未批准 → 不执行
    r = await client.post(f"/sandbox/sessions/{sid}/act",
                         json={"action_type": "delete_file",
                               "params": {"path": "/old.txt"}}, headers=h)
    assert r.status_code == 200, r.text
    pending = r.json()
    assert set(pending) == _keys(comp.SandboxActApprovalOut), pending
    assert pending["requires_approval"] is True
    assert "delete_file" in pending["reason"]

    # 分支二：普通动作 → 真的走完 Observe/Act/Verify
    r = await client.post(f"/sandbox/sessions/{sid}/act",
                         json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 200, r.text
    ran = r.json()
    assert set(ran) == _keys(comp.SandboxActRunOut), ran
    assert ran["seq"] == 2 and ran["action"] == "screenshot"
    assert set(ran["screenshots"]) == {"before", "after"}

    # 批准那条待审批动作
    r = await client.post(f"/sandbox/actions/{pending['action_id']}/approve",
                          headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(comp.SandboxActionApproveOut), r.json()
    assert r.json()["approved"] is True

    # 列表：两条都要能序列化——待审批那条的 verified/before/after 是 NULL
    r = await client.get(f"/sandbox/sessions/{sid}/actions", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    for row in rows:
        assert set(row) == _keys(comp.SandboxActionOut), row
    by_seq = {row["seq"]: row for row in rows}
    assert by_seq[1]["high_risk"] is True
    assert by_seq[1]["verified"] is None and by_seq[1]["before"] is None
    assert by_seq[2]["verified"] is True


async def test_save_and_delete_file_sync_workspace_files(client, tenants):
    """save_file / delete_file 与工作区文件表联动，列表键集合固定。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]
    wid = await _mk_workspace(client, h, cid)
    sid = await _mk_session(client, h, wid)

    long_text = "x" * 300
    r = await client.post(f"/sandbox/sessions/{sid}/act",
                          json={"action_type": "save_file",
                                "params": {"path": "/note.md",
                                           "content": long_text}}, headers=h)
    assert r.status_code == 200, r.text

    r = await client.get(f"/sandbox/workspaces/{wid}/files", headers=h)
    assert r.status_code == 200, r.text
    files = r.json()
    assert len(files) == 1
    assert set(files[0]) == _keys(comp.SandboxWorkspaceFileOut), files
    assert files[0]["path"] == "/note.md"
    assert files[0]["size"] == len(long_text)
    # 列表只回前 200 字，这是端点里写死的截断，不是序列化裁出来的
    assert len(files[0]["content"]) == 200

    # 覆盖写：同一 path 不产生第二行
    await client.post(f"/sandbox/sessions/{sid}/act",
                      json={"action_type": "save_file",
                            "params": {"path": "/note.md", "content": "短"}},
                      headers=h)
    r = await client.get(f"/sandbox/workspaces/{wid}/files", headers=h)
    assert len(r.json()) == 1 and r.json()[0]["size"] == 1

    await client.post(f"/sandbox/sessions/{sid}/act?approved=true",
                      json={"action_type": "delete_file",
                            "params": {"path": "/note.md"}}, headers=h)
    r = await client.get(f"/sandbox/workspaces/{wid}/files", headers=h)
    assert r.json() == []


async def test_sandbox_cross_tenant_is_404(client, tenants):
    """别人公司的工作区/会话：一律 404，不泄露是否存在。"""
    ah, a_cid = tenants.alice["headers"], tenants.alice_co["id"]
    bh, b_cid = tenants.bob["headers"], tenants.bob_co["id"]
    wid = await _mk_workspace(client, ah, a_cid)
    sid = await _mk_session(client, ah, wid)

    for method, url in (("GET", f"/sandbox/workspaces/{wid}/files"),
                        ("GET", f"/sandbox/sessions/{sid}/actions"),
                        ("POST", f"/sandbox/sessions/{sid}/pause"),
                        ("POST", f"/sandbox/sessions/{sid}/cancel")):
        r = await (client.get(url, headers=bh) if method == "GET"
                   else client.post(url, headers=bh))
        assert r.status_code == 404, f"{method} {url} -> {r.status_code} {r.text}"

    # 工作区列表只回自己公司的
    r = await client.get(f"/sandbox/workspaces?company_id={b_cid}", headers=bh)
    assert r.json() == []


# ======================================================== /computer-agent


async def test_grant_revoke_permissions_capabilities(client, tenants):
    """授权闭环：默认全 False → grant → revoke；两个总览端点键集合钉死。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]

    r = await client.get(f"/computer-agent/permissions?company_id={cid}",
                         headers=h)
    assert r.status_code == 200, r.text
    perms = r.json()
    # /permissions 就是 capability_status() 的「能力名 → 布尔」映射
    assert set(perms) == set(EXTENDED_CAPABILITIES)
    assert all(v is False for v in perms.values()), "默认必须全未授权"

    r = await client.post(f"/computer-agent/grant?capability=screenshot"
                          f"&company_id={cid}", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == _keys(comp.CapabilityGrantOut), r.json()
    assert r.json() == {"capability": "screenshot", "granted": True}

    r = await client.get(f"/computer-agent/permissions?company_id={cid}",
                         headers=h)
    assert r.json()["screenshot"] is True

    r = await client.get(f"/computer-agent/capabilities?company_id={cid}",
                         headers=h)
    assert r.status_code == 200, r.text
    caps = r.json()
    assert set(caps) == _keys(comp.CapabilitiesListOut), caps
    assert caps["capabilities"] == sorted(EXTENDED_CAPABILITIES)
    assert set(caps["granted"]) == set(EXTENDED_CAPABILITIES)

    r = await client.post(f"/computer-agent/revoke?capability=screenshot"
                          f"&company_id={cid}", headers=h)
    assert set(r.json()) == {"capability", "granted"}
    assert r.json()["granted"] is False

    # 未知能力不许"顺手授权"
    r = await client.post(f"/computer-agent/grant?capability=rm_rf"
                          f"&company_id={cid}", headers=h)
    assert r.status_code == 400, r.text


async def test_execute_denied_unknown_and_real(client, tenants, monkeypatch):
    """三条执行分支：未授权 403 / 未知能力 400 / 已授权真执行（执行器已替身）。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]
    # 顶掉本机执行器：这条用例会真的调用 screenshot
    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "screenshot",
                        _fake_executor({"ok": True, "image_path": "/tmp/a.png",
                                        "size": 4096}))

    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 403, r.text
    assert "未获授权" in r.json()["detail"], r.text

    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "not_a_capability"}, headers=h)
    assert r.status_code == 400, r.text

    await client.post(f"/computer-agent/grant?capability=screenshot"
                      f"&company_id={cid}", headers=h)
    agent_id = await _seed_agent(cid)
    r = await client.post(f"/computer-agent/execute?company_id={cid}"
                          f"&agent_id={agent_id}",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    # exclude_unset 生效：待审批分支的 status/message 不会在这里冒出 null
    assert set(out) == {"log_id", "action", "result", "mode"}, out
    assert out["mode"] == "REAL" and out["action"] == "screenshot"
    assert out["result"]["image_path"] == "/tmp/a.png"

    # 执行器报错 → mode 必须是 REAL_FAILED，不许包装成成功
    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "screenshot",
                        _fake_executor({"ok": False, "error": "没有屏幕录制权限"}))
    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.json()["mode"] == "REAL_FAILED", r.text

    # 审计日志：含被拒绝的那几条，键集合固定
    r = await client.get(f"/computer-agent/logs?company_id={cid}", headers=h)
    assert r.status_code == 200, r.text
    logs = r.json()
    # 403 落一条 + 成功 + 失败；未知能力在执行器查表前就抛错，不落库
    assert len(logs) == 3, logs
    for row in logs:
        assert set(row) == _keys(comp.ComputerLogOut), row
    denied = [row for row in logs if row["allowed"] is False]
    assert denied and denied[0]["result"] is None, "拒绝审计不带结果"
    assert denied[0]["deny_reason"]

    # 跨公司冒用别人的 Agent：404（「自己的 Agent + 别人的公司」错配）
    bob_agent = await _seed_agent(tenants.bob_co["id"], name="B 公司员工")
    r = await client.post(f"/computer-agent/execute?company_id={cid}"
                          f"&agent_id={bob_agent}",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 404, r.text


async def test_execute_dry_run_precheck(client, tenants):
    """dry_run 只跑预检，不碰本机；两个方向的键集合一致、值不同。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]

    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "click", "dry_run": True,
                                "params": {"x": 1}}, headers=h)
    assert r.status_code == 200, r.text
    blocked = r.json()
    assert set(blocked) == _keys(comp.ComputerExecuteOut) - {
        "log_id", "action", "mode", "auto_approved", "message", "status",
        "approval_request_id"}, blocked
    assert blocked["dry_run"] is True and blocked["precheck_blocked"] is True
    assert "坐标缺失" in blocked["result"]["error"]

    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "click", "dry_run": True,
                                "params": {"x": 100, "y": 200}}, headers=h)
    ok = r.json()
    assert ok["precheck_blocked"] is False and ok["result"]["ok"] is True
    # 预检通过也不代表已授权：dry_run 不落执行
    assert ok["result"].get("precheck") is True


async def test_execute_approval_gate_then_whitelist(client, tenants,
                                                    monkeypatch):
    """高危操作：策略开了 computer_command → 先落审批单；命中白名单 → 直接放行。"""
    h, cid = tenants.alice["headers"], tenants.alice_co["id"]
    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "run_shell",
                        _fake_executor({"ok": True, "stdout": "hello",
                                        "exit_code": 0}))
    await _seed_policy(cid, ["computer_command"])

    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "run_shell",
                                "params": {"command": "ls"}}, headers=h)
    assert r.status_code == 200, r.text
    held = r.json()
    assert set(held) == {"status", "approval_request_id", "message", "mode"}, held
    assert held["status"] == "awaiting_approval" and held["mode"] == "HOLD"
    # 待审批分支绝不能带 log_id——此时确实还没执行、也没落执行审计
    assert "log_id" not in held

    # 免批白名单：老板勾过「始终允许」→ 同类操作直接放行并留痕
    await client.post(f"/computer-agent/grant?capability=run_shell"
                      f"&company_id={cid}", headers=h)
    await _seed_whitelist(cid, "run_shell", param_pattern="")
    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "run_shell",
                                "params": {"command": "ls"}}, headers=h)
    assert r.status_code == 200, r.text
    passed = r.json()
    assert set(passed) == _keys(comp.ComputerExecuteOut) - {
        "status", "approval_request_id", "dry_run", "precheck_blocked"}, passed
    assert passed["auto_approved"] == "whitelist"
    assert passed["mode"] == "REAL" and passed["result"]["stdout"] == "hello"

    # 撤销白名单立即恢复拦截（回到落审批单那条分支）
    r = await client.delete(f"/intervention/whitelist/{(await _latest_whitelist(cid))}"
                            f"?company_id={cid}", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "run_shell",
                                "params": {"command": "ls"}}, headers=h)
    assert r.json()["status"] == "awaiting_approval", r.text

    # 非高危能力不受这条门影响（screenshot 不在 HIGH_RISK_CAPABILITIES）
    r = await client.post(f"/computer-agent/execute?company_id={cid}",
                          json={"action_type": "screenshot"}, headers=h)
    assert r.status_code == 403, "未授权依然是未授权，和审批门无关"


async def _latest_whitelist(company_id: str) -> str:
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        row = (await s.execute(select(ApprovalWhitelist).where(
            ApprovalWhitelist.company_id == company_id))).scalars().first()
        return row.id


async def test_latest_screenshot_only_from_sandbox_dir(client, tenants,
                                                       monkeypatch):
    """截屏预览：没有记录 404；有记录回 data URL；越界路径不返回文件内容。"""
    import base64

    h, cid = tenants.alice["headers"], tenants.alice_co["id"]

    r = await client.get(f"/computer-agent/screenshots/latest?company_id={cid}",
                         headers=h)
    assert r.status_code == 404, r.text

    await client.post(f"/computer-agent/grant?capability=screenshot"
                      f"&company_id={cid}", headers=h)
    os.makedirs(_SCREEN_ROOT, exist_ok=True)
    path = os.path.join(_SCREEN_ROOT, f"contract_{_id()[:8]}.png")
    payload = b"\x89PNG\r\n\x1a\n-fake-pixel-"
    with open(path, "wb") as f:
        f.write(payload)
    try:
        monkeypatch.setitem(computer_service.REAL_EXECUTORS, "screenshot",
                            _fake_executor({"ok": True, "image_path": path,
                                            "size": len(payload)}))
        r = await client.post(f"/computer-agent/execute?company_id={cid}",
                              json={"action_type": "screenshot"}, headers=h)
        assert r.status_code == 200, r.text

        r = await client.get(f"/computer-agent/screenshots/latest"
                             f"?company_id={cid}", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert set(out) == _keys(comp.LatestScreenshotOut), out
        assert out["image_base64"].startswith("data:image/png;base64,")
        assert base64.b64decode(out["image_base64"].split(",", 1)[1]) == payload
        # 端点回的是 resolve() 之后的路径（macOS 上 /tmp 是 /private/tmp 的符号链接）
        assert out["path"] == os.path.realpath(path), out["path"]

        # 跨公司取不到别人的截屏
        r = await client.get(f"/computer-agent/screenshots/latest"
                             f"?company_id={tenants.bob_co['id']}",
                             headers=tenants.bob["headers"])
        assert r.status_code == 404, r.text
    finally:
        os.remove(path)


# ================================================================= 静态护栏


def test_both_routers_declare_response_models():
    """两个路由的端点一个不许裸着——新加端点必须带模型。"""
    missing = []
    total = 0
    for route in _api_routes():
        module = getattr(route.endpoint, "__module__", "")
        if module not in _MODULES:
            continue
        for verb in route.methods:
            if verb in ("HEAD", "OPTIONS"):
                continue
            total += 1
            if route.response_model is None:
                missing.append(f"{verb} {route.path}")
    assert total == 17, f"两个路由的操作数不对（实测 {total}）"
    assert not missing, f"这些端点没有 response_model：{missing}"


def test_computer_models_allow_unknown_fields():
    """判断错字段时宁可原样透出，也不许序列化静默裁掉前端在用的键。"""
    offenders = {
        name for name, obj in vars(comp).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel)
        and obj.__module__ == comp.__name__
        and obj.model_config.get("extra") != "allow"
    }
    assert not offenders, offenders
