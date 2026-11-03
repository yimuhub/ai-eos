"""远程执行设备：接入、隧道领取、下发执行。

盯住三件最容易出事的地方：
1. 一次性接入码不能被复用，库里不能出现明文；
2. 同一条命令不能被两个 sidecar 各执行一遍（领取必须是 CAS）；
3. **指名打到某台机器时，那台机器不在就绝不回落到后端本机**——
   在错误的电脑上改了另一个文件，比这次操作失败严重得多。
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.computer_agent import ComputerActionLog, ComputerPermission
from app.db.session import AsyncSessionLocal
from app.models.computer_device import ComputerDevice, ComputerDispatch
from app.models.plugin import ComputerPolicy
from app.services import computer_dispatch, computer_policy, computer_service

CAPS = ["run_shell", "read_file", "write_file"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_device(cid: str, name: str = "设计部的小张",
                       capabilities: list[str] | None = None) -> tuple[str, str]:
    """登记并接入一台设备，返回 (device_id, 设备密钥)。"""
    async with AsyncSessionLocal() as s:
        device, code = await computer_dispatch.create_device(
            s, company_id=cid, name=name)
        device_id = device.id
    async with AsyncSessionLocal() as s:
        enrolled, secret = await computer_dispatch.enroll(
            s, code=code, platform="darwin", hostname="mbp-01",
            agent_version="1.0.0",
            capabilities=capabilities if capabilities is not None else CAPS)
    assert enrolled.id == device_id
    return device_id, secret


async def _device_row(device_id: str) -> ComputerDevice:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(ComputerDevice).where(ComputerDevice.id == device_id)
        )).scalars().one()


async def _set_device(device_id: str, **values) -> None:
    """改设备行。必须「在同一个会话里查出来再改」：拿另一个会话的游离实例
    赋值再 commit，改动会静默丢掉——测试就成了「以为设备离线了，其实没有」。"""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(ComputerDevice).where(
            ComputerDevice.id == device_id))).scalars().one()
        for key, value in values.items():
            setattr(row, key, value)
        await s.commit()


async def _grant_and_full_control(cid: str, capability: str = "run_shell") -> None:
    """能力授权 + 完全控制：让这批用例只关心"落到哪台机器"，不掺审批门。"""
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability=capability, granted=True))
        s.add(ComputerPolicy(company_id=cid, mode="full",
                             full_control_until=_utcnow() + timedelta(minutes=10),
                             auto_dirs=[], auto_commands=[], auto_apps=[],
                             notify_on_finish=True))
        await s.commit()


def _fast_dispatch(monkeypatch) -> None:
    """把下发窗口压到 1 秒：否则"设备没回报"这条分支要真等两分钟才测得到。"""
    monkeypatch.setattr(computer_dispatch, "DEFAULT_DISPATCH_TIMEOUT",
                        timedelta(seconds=1))
    monkeypatch.setattr(computer_dispatch, "MIN_DISPATCH_TIMEOUT",
                        timedelta(seconds=1))
    monkeypatch.setattr(computer_dispatch, "RESULT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(computer_dispatch, "POLL_INTERVAL", 0.05)


async def _run_device_side(device_id: str, *, ok: bool = True,
                           result: dict | None = None,
                           wait: float = 8.0) -> dict | None:
    """扮演设备端：领一条命令并回报。返回领到的任务（没领到就是 None）。"""
    async with AsyncSessionLocal() as s:
        device = (await s.execute(
            select(ComputerDevice).where(ComputerDevice.id == device_id)
        )).scalars().one()
        task = await computer_dispatch.claim_with_wait(s, device, wait=wait)
        if task is None:
            return None
        await computer_dispatch.report(s, device, task["dispatch_id"], ok=ok,
                                       result=result)
        return task


# ============================================================ 接入与鉴权


async def test_enrollment_is_one_shot_and_never_stored_in_plaintext(client,
                                                                    tenants):
    cid = tenants.alice_co["id"]
    h = tenants.alice["headers"]

    r = await client.post(f"/computer-devices?company_id={cid}",
                          json={"name": "会议室的 Mac mini"}, headers=h)
    assert r.status_code == 200, r.text
    created = r.json()
    code = created["enroll_code"]
    device_id = created["device"]["id"]
    assert created["device"]["enrolled"] is False
    assert created["device"]["online"] is False
    # 没接入的设备不能被设成默认执行机——否则每条操作都会撞"设备未接入"
    r = await client.post(f"/computer-devices/{device_id}/set-default"
                          f"?company_id={cid}", headers=h)
    assert r.status_code == 409, r.text

    ok = await client.post("/computer-devices/enroll",
                           json={"code": code, "platform": "darwin",
                                 "hostname": "mini-01", "agent_version": "1.0.0",
                                 "capabilities": CAPS})
    assert ok.status_code == 200, ok.text
    secret = ok.json()["secret"]
    assert secret

    # 同一个接入码再用一次：换不出第二个密钥
    again = await client.post("/computer-devices/enroll",
                              json={"code": code, "platform": "darwin",
                                    "capabilities": CAPS})
    assert again.status_code == 404, again.text

    row = await _device_row(device_id)
    assert row.enroll_code_hash is None
    # 落库的是 SHA-256（64 位十六进制），不是明文
    assert len(row.secret_hash) == 64
    assert all(c in "0123456789abcdef" for c in row.secret_hash)
    assert secret not in row.secret_hash


async def test_same_name_device_is_rejected(client, tenants):
    """默认执行机靠名字选，重名会选错机器。"""
    cid = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    first = await client.post(f"/computer-devices?company_id={cid}",
                              json={"name": "老板的 MacBook"}, headers=h)
    assert first.status_code == 200, first.text
    dup = await client.post(f"/computer-devices?company_id={cid}",
                            json={"name": "老板的 MacBook"}, headers=h)
    assert dup.status_code == 409, dup.text


async def test_tunnel_requires_the_device_key(client, tenants):
    _, secret = await _make_device(tenants.alice_co["id"])

    assert (await client.post("/computer-devices/tunnel/claim"
                              )).status_code == 401
    wrong = await client.post("/computer-devices/tunnel/claim",
                              headers={"X-AIEOS-Device-Key": "not-mine"})
    assert wrong.status_code == 401, wrong.text
    mine = await client.post("/computer-devices/tunnel/claim?wait=0",
                             headers={"X-AIEOS-Device-Key": secret})
    assert mine.status_code == 200, mine.text
    assert mine.json() == {"task": None}  # 队列空，但鉴权过了


async def test_heartbeat_marks_online_and_drops_invented_capabilities(
        client, tenants):
    cid = tenants.alice_co["id"]
    device_id, secret = await _make_device(cid)
    async with AsyncSessionLocal() as s:
        row = await _device_row(device_id)
        row.capabilities = []
        row.last_seen_at = _utcnow() - timedelta(minutes=10)
        await s.commit()

    r = await client.post("/computer-devices/tunnel/heartbeat",
                          headers={"X-AIEOS-Device-Key": secret},
                          json={"platform": "linux", "hostname": "box-2",
                                "agent_version": "1.0.1",
                                "capabilities": ["run_shell", "rm_rf_everything"]})
    assert r.status_code == 200, r.text
    # 设备虚报一个后端不认识的能力 = 给自己开了一条策略里没有的动作类型
    assert r.json()["capabilities"] == ["run_shell"]

    listed = await client.get(f"/computer-devices?company_id={cid}",
                              headers=tenants.alice["headers"])
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["default_device_id"] is None
    mine = next(d for d in body["devices"] if d["id"] == device_id)
    assert mine["online"] is True and mine["platform"] == "linux"
    assert mine["agent_version"] == "1.0.1"


async def test_expired_enrollment_code_can_be_renewed(client, tenants):
    cid = tenants.alice_co["id"]
    h = tenants.alice["headers"]
    r = await client.post(f"/computer-devices?company_id={cid}",
                          json={"name": "过期码测试"}, headers=h)
    code = r.json()["enroll_code"]
    device_id = r.json()["device"]["id"]
    await _set_device(device_id,
                      enroll_expires_at=_utcnow() - timedelta(minutes=1))

    stale = await client.post("/computer-devices/enroll",
                              json={"code": code, "platform": "darwin",
                                    "capabilities": CAPS})
    assert stale.status_code == 410, stale.text

    renewed = await client.post(f"/computer-devices/{device_id}/renew"
                                f"?company_id={cid}", headers=h)
    assert renewed.status_code == 200, renewed.text
    fresh = renewed.json()["enroll_code"]
    assert fresh != code
    ok = await client.post("/computer-devices/enroll",
                           json={"code": fresh, "platform": "darwin",
                                 "capabilities": CAPS})
    assert ok.status_code == 200, ok.text
    # 旧码即使还没被用过也不能再换出密钥（新码把它顶掉了）
    old = await client.post("/computer-devices/enroll",
                            json={"code": code, "capabilities": CAPS})
    assert old.status_code == 404, old.text


# ============================================================ 领取与回报


async def test_one_command_is_claimed_by_exactly_one_sidecar(client, tenants):
    """两个 sidecar 同时轮询，同一条命令只能被执行一遍。"""
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid)
    async with AsyncSessionLocal() as s:
        device = await _device_row(device_id)
        row = await computer_dispatch.dispatch(
            s, device=device, company_id=cid, capability="run_shell",
            params={"command": "ls"})
        dispatch_id = row.id

    first, second = await asyncio.gather(
        _run_device_side(device_id, wait=0, result={"ok": True, "stdout": "apps"}),
        _run_device_side(device_id, wait=0, result={"ok": True, "stdout": "apps"}))
    claimed = [c for c in (first, second) if c is not None]
    assert len(claimed) == 1, (first, second)
    assert claimed[0]["dispatch_id"] == dispatch_id

    # 重复回报（网络重试）不报错，也不把已成功的改成失败
    async with AsyncSessionLocal() as s:
        device = await _device_row(device_id)
        retried = await computer_dispatch.report(
            s, device, dispatch_id, ok=False, error="我挂了")
    assert retried["accepted"] is False
    async with AsyncSessionLocal() as s:
        after = (await s.execute(select(ComputerDispatch).where(
            ComputerDispatch.id == dispatch_id))).scalars().one()
    assert after.status == "succeeded" and after.result["ok"] is True


async def test_report_rejects_another_devices_dispatch(client, tenants):
    cid = tenants.alice_co["id"]
    mine, _ = await _make_device(cid, name="我的机器")
    other, _ = await _make_device(cid, name="同事的机器")
    async with AsyncSessionLocal() as s:
        device = await _device_row(mine)
        row = await computer_dispatch.dispatch(
            s, device=device, company_id=cid, capability="run_shell",
            params={"command": "ls"})
    async with AsyncSessionLocal() as s:
        thief = (await s.execute(select(ComputerDevice).where(
            ComputerDevice.id == other))).scalars().one()
        try:
            await computer_dispatch.report(s, thief, row.id, ok=True,
                                           result={"ok": True})
            raise AssertionError("别的设备回报了不属于自己的命令却没被拒")
        except LookupError:
            pass


# ============================================================ 落到哪台机器


async def test_execute_on_device_goes_through_the_tunnel_not_local(
        client, tenants, monkeypatch):
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid)
    await _grant_and_full_control(cid, "run_shell")

    local_calls: list[dict] = []

    async def _spy(params: dict) -> dict:
        local_calls.append(params)
        return {"ok": True, "stdout": "本机跑的"}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "run_shell", _spy)

    side = asyncio.create_task(_run_device_side(
        device_id, result={"ok": True, "stdout": "在设备的机器上跑的",
                           "exit_code": 0}))
    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="run_shell",
            params={"command": "ls"}, device_id=device_id)
    task = await side

    assert local_calls == [], "远程执行回落到了本机"
    assert task is not None, "命令没进隧道"
    assert outcome["ok"] is True, outcome
    assert outcome["result"]["stdout"] == "在设备的机器上跑的"
    assert outcome["result"]["remote"] is True
    assert outcome["device"]["name"] == "设计部的小张"

    # 审计行被设备的真实结果覆盖，并且和下发记录互相对应
    async with AsyncSessionLocal() as s:
        log = (await s.execute(select(ComputerActionLog).where(
            ComputerActionLog.id == outcome["log_id"]))).scalars().one()
        dispatch_row = (await s.execute(select(ComputerDispatch).where(
            ComputerDispatch.id == task["dispatch_id"]))).scalars().one()
    assert log.result["stdout"] == "在设备的机器上跑的", log.result
    assert log.result["device"]["id"] == device_id
    assert dispatch_row.log_id == log.id and dispatch_row.status == "succeeded"


async def test_offline_device_never_falls_back_to_local(client, tenants,
                                                        monkeypatch):
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid)
    await _grant_and_full_control(cid, "run_shell")
    await _set_device(device_id,
                      last_seen_at=_utcnow() - timedelta(minutes=5))

    ran: list[str] = []

    async def _spy(params: dict) -> dict:
        ran.append("本机执行了")
        return {"ok": True}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "run_shell", _spy)

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="run_shell",
            params={"command": "ls"}, device_id=device_id)
    assert ran == []
    assert outcome["ok"] is False and outcome["allowed"] is False
    assert "离线" in outcome["deny_reason"]
    async with AsyncSessionLocal() as s:
        logs = (await s.execute(select(ComputerActionLog))).scalars().all()
    assert any(log.allowed is False and "离线" in (log.deny_reason or "")
               for log in logs), [log.deny_reason for log in logs]
    # 没派发的命令不该留在队列里
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(ComputerDispatch))).scalars().all() == []


async def test_unsupported_capability_on_device_is_refused(client, tenants):
    """设备只上报了 run_shell：write_file 不能派给它。"""
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid, capabilities=["run_shell"])
    await _grant_and_full_control(cid, "write_file")

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="write_file",
            params={"path": "/tmp/x.txt", "content": "hi"},
            device_id=device_id)
    assert outcome["ok"] is False
    assert "不支持 write_file" in outcome["deny_reason"]
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(ComputerDispatch))).scalars().all() == []


async def test_company_default_device_routes_every_call(
        client, tenants, monkeypatch):
    """设了默认执行机以后，没指名设备的调用（聊天那条路）也落到它上面。"""
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid)
    await _grant_and_full_control(cid, "run_shell")

    local_calls: list[dict] = []

    async def _spy(params: dict) -> dict:
        local_calls.append(params)
        return {"ok": True, "stdout": "本机跑的"}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "run_shell", _spy)
    async with AsyncSessionLocal() as s:
        await computer_policy.set_default_device(s, company_id=cid,
                                                 device_id=device_id)

    side = asyncio.create_task(_run_device_side(
        device_id, result={"ok": True, "stdout": "默认机跑的"}))
    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="run_shell", params={"command": "ls"})
    await side
    assert local_calls == []
    assert outcome["result"]["stdout"] == "默认机跑的"

    # 取消默认 → 回到本机直连（改造前的行为）
    async with AsyncSessionLocal() as s:
        assert await computer_policy.clear_default_device(s, cid, device_id)
    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="run_shell", params={"command": "ls"})
    assert outcome["result"]["stdout"] == "本机跑的"
    assert "device" not in outcome


async def test_silent_device_times_out_and_says_so(client, tenants, monkeypatch):
    """命令派出去了但设备再不回报：判过期，如实说"没回报"，不粉饰成成功或失败。"""
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid)
    await _grant_and_full_control(cid, "run_shell")
    _fast_dispatch(monkeypatch)

    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=cid, capability="run_shell",
            params={"command": "ls"}, device_id=device_id)
    assert outcome["ok"] is False
    assert outcome["result"]["expired"] is True
    assert "没有回报" in outcome["result"]["error"]
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(ComputerDispatch))).scalars().one()
        assert row.status == "expired"
        log = (await s.execute(select(ComputerActionLog).where(
            ComputerActionLog.id == row.log_id))).scalars().one()
    assert log.result["expired"] is True, log.result


async def test_revoke_kills_the_queue_and_the_default(client, tenants):
    cid = tenants.alice_co["id"]
    device_id, secret = await _make_device(cid)
    await _grant_and_full_control(cid, "run_shell")
    async with AsyncSessionLocal() as s:
        device = await _device_row(device_id)
        await computer_dispatch.dispatch(s, device=device, company_id=cid,
                                         capability="run_shell",
                                         params={"command": "ls"})
        await computer_policy.set_default_device(s, company_id=cid,
                                                 device_id=device_id)

    h = tenants.alice["headers"]
    r = await client.post(f"/computer-devices/{device_id}/revoke"
                          f"?company_id={cid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cancelled_dispatches"] == 1

    gone = await client.post("/computer-devices/tunnel/claim?wait=0",
                             headers={"X-AIEOS-Device-Key": secret})
    assert gone.status_code == 401, gone.text

    listed = (await client.get(f"/computer-devices?company_id={cid}",
                               headers=h)).json()
    # 默认执行机被吊销必须一起清掉，否则之后每条操作都撞"设备已吊销"
    assert listed["default_device_id"] is None
    assert next(d for d in listed["devices"]
                if d["id"] == device_id)["revoked"] is True


async def test_another_company_cannot_point_at_my_device(client, tenants,
                                                         monkeypatch):
    """设备属于 A 公司：B 公司指名用它执行 = 查不到，而不是替别人操作机器。"""
    device_id, _ = await _make_device(tenants.alice_co["id"])
    await _grant_and_full_control(tenants.bob_co["id"], "run_shell")

    async def _spy(params: dict) -> dict:
        return {"ok": True, "stdout": "本机跑的"}

    monkeypatch.setitem(computer_service.REAL_EXECUTORS, "run_shell", _spy)
    async with AsyncSessionLocal() as s:
        outcome = await computer_service.execute_computer_action(
            s, company_id=tenants.bob_co["id"], capability="run_shell",
            params={"command": "ls"}, device_id=device_id)
    assert outcome["ok"] is False and outcome["allowed"] is False
    assert "不存在" in outcome["deny_reason"]

    # 老板侧接口同样按公司隔离：B 公司看不到 A 公司的设备
    listed = (await client.get(f"/computer-devices?company_id={tenants.bob_co['id']}",
                               headers=tenants.bob["headers"])).json()
    assert listed["devices"] == []


async def test_approval_freezes_the_target_device(client, tenants):
    """审批单必须写清落在哪台机器，并把设备冻进 payload：
    批准后执行的是建单那一刻的那台，不是"当时那台现在怎么样了"。"""
    cid = tenants.alice_co["id"]
    device_id, _ = await _make_device(cid, name="财务的旧电脑")
    async with AsyncSessionLocal() as s:
        s.add(ComputerPermission(id=str(uuid.uuid4()), company_id=cid,
                                 capability="write_file", granted=True))
        s.add(ComputerPolicy(company_id=cid, mode="default", auto_dirs=[],
                             auto_commands=[], auto_apps=[],
                             notify_on_finish=True))
        await s.commit()

    async with AsyncSessionLocal() as s:
        gate = await computer_service.approval_gate(
            s, company_id=cid, capability="write_file",
            params={"path": "/tmp/note.txt", "content": "改成这样\n"},
            device_id=device_id)
    assert gate["action"] == "hold", gate
    assert gate["device"]["name"] == "财务的旧电脑"

    from app.models.intervention import ApprovalRequest

    async with AsyncSessionLocal() as s:
        req = (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.id == gate["approval_request_id"]))).scalars().one()
    payload = req.payload
    assert payload["device_id"] == device_id
    assert payload["device_name"] == "财务的旧电脑"
    # 远程的 diff 不能拿本机同名文件去算——那是假预览
    assert payload["preview"]["available"] is False
    assert "财务的旧电脑" in payload["preview"]["reason"]
    assert "设备「财务的旧电脑」" in req.reason
