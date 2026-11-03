#!/usr/bin/env python3
"""远程执行设备的端到端验证：真实起两个进程（后端 + sidecar）跑一遍派单。

## 为什么需要它

单元测试里的「设备」是假的：TestClient 直接调隧道接口，领命令、回报结果都由
测试自己扮演。那条链路真正出问题的是**两个进程之间的部分**——sidecar 的长轮询
超时与服务端在线窗口打架、命令派出去却没人回报、吊销后本机还在傻等。
所以这里跑的是真的：临时 SQLite 起一个 uvicorn，另起一个 sidecar 进程注册成设备，
从老板侧接口下命令，断言文件真的写在了那台机器的磁盘上。

## 断言的六件事

1. 远程 `read_file` 成功，且响应里写明在哪台机器上跑的（不是后端本机）。
2. 写操作被审批门挂起：批准前磁盘上没有那个文件；审批单冻结目标设备、
   不演算本机 diff；批准后由 sidecar 真的写出。
3. 后端批准了也没用——落在 `--allow-root` 之外，设备端第二把锁照样拒绝。
4. 没接入/离线的设备被如实拒绝，绝不回落到后端本机执行。
5. 吊销清掉默认执行机，被吊销的 sidecar 领不到命令（本机报密钥失效）。
6. 审计里既有成功也有拒绝，每条都带执行机器名。

## 安全约束

全程临时目录：临时 SQLite、临时存储、临时 `.env`、临时 sidecar 配置，
绝不碰 `apps/api/data/aieos.db`、`apps/api/.env` 或 `~/.aieos`。
随机空闲端口，结束后自动清理（`KEEP=1` 保留现场排查）。

## 用法

```bash
apps/api/.venv/bin/python tools/e2e_remote_devices.py     # 约 40 秒，退出码 0 = 全绿
KEEP=1 apps/api/.venv/bin/python tools/e2e_remote_devices.py   # 保留临时目录排查
```
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API_DIR = REPO / "apps" / "api"
VENV_PY = str(API_DIR / ".venv" / "bin" / "python")
SIDECAR = str(REPO / "apps" / "device_agent" / "aieos_sidecar.py")
TIMEOUT = 90.0


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status, self.body = status, body


def call(base, method, path, body=None, token=None, timeout=TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        status = e.code
    if status >= 400:
        raise ApiError(status, text)
    return json.loads(text) if text.strip() else None


ROWS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    ROWS.append((name, bool(ok), detail))
    print(f"{'✔' if ok else '✘'} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aieos_e2e_device_"))
    work = tmp / "device-work"          # = sidecar 唯一的可写根目录
    work.mkdir()
    (work / "seed.txt").write_text("设备上的原件内容\n", encoding="utf-8")
    env_file = tmp / "env"
    env_file.mkdir()
    (env_file / ".env").write_text("", encoding="utf-8")

    api_env = dict(os.environ)
    api_env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'api.db'}",
        "STORAGE_DIR": str(tmp / "storage"),
        "DATA_DIR": str(tmp / "data"),
        "AIEOS_ENV_FILE": str(env_file / ".env"),
        "JWT_SECRET": secrets.token_hex(32),
        "AIEOS_ENV": "development",
        "DEBUG": "false",
        "PYTHONUNBUFFERED": "1",
    })
    (tmp / "storage").mkdir()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    api_log = open(tmp / "api.log", "w", encoding="utf-8")
    api = subprocess.Popen([VENV_PY, "-m", "uvicorn", "app.main:app",
                            "--host", "127.0.0.1", "--port", str(port),
                            "--log-level", "warning"],
                           cwd=str(API_DIR), env=api_env,
                           stdout=api_log, stderr=subprocess.STDOUT)
    sidecar: dict = {}
    try:
        return run(base, tmp, work, api_env, api, sidecar)
    finally:
        runner = sidecar.get("runner")
        if runner is not None:
            runner.terminate()
            try:
                runner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                runner.kill()
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
        api_log.close()
        print(f"\n临时目录：{tmp}（Ctrl 保留：{os.environ.get('KEEP')}）")
        if os.environ.get("KEEP") != "1":
            shutil.rmtree(tmp, ignore_errors=True)


def run(base: str, tmp: Path, work: Path, api_env: dict, api, sidecar: dict) -> int:
    token = None
    for _ in range(90):
        if api.poll() is not None:
            print("后端进程退出了：\n" + (tmp / "api.log").read_text()[-3000:])
            return 1
        try:
            call(base, "GET", "/health", timeout=3)
            break
        except Exception:
            time.sleep(1)
    else:
        print("后端 90 秒没就绪")
        return 1

    suffix = secrets.token_hex(3)
    email = f"dev_{suffix}@example.com"
    reg = call(base, "POST", "/auth/register",
               {"email": email, "username": f"dev_{suffix}",
                "password": "DevPassw0rd", "display_name": "设备验证"}, token)
    login = call(base, "POST", "/auth/login",
                 {"identity": email, "password": "DevPassw0rd"}, token)
    token = login["access_token"]
    company = call(base, "POST", "/companies", {"name": "远程设备验证公司"}, token)
    cid = company["id"]
    q = f"?company_id={cid}"
    check("后端启动 + 注册/登录/建公司", bool(cid), base)

    # ---- 策略：默认审批档；授权 read_file / write_file ----
    call(base, "PUT", "/computer-agent/policy" + q, {"mode": "default"}, token)
    for cap in ("read_file", "write_file", "run_shell"):
        call(base, "POST", f"/computer-agent/grant{q}&capability={cap}", {}, token)
    perms = call(base, "GET", "/computer-agent/permissions" + q, token=token)
    check("三档策略=默认审批 + 能力已授权",
          all(perms.get(c) for c in ("read_file", "write_file")), str(perms))

    # ---- 建设备 → sidecar 真实进程接入 ----
    created = call(base, "POST", "/computer-devices" + q,
                  {"name": "验证用的 MacBook"}, token)
    device_id, code = created["device"]["id"], created["enroll_code"]
    sidecar_env = dict(api_env)
    sidecar_env["AIEOS_SIDECAR_CONFIG"] = str(tmp / "sidecar.json")
    logf = open(tmp / "sidecar.log", "w", encoding="utf-8")
    enroll = subprocess.run([VENV_PY, SIDECAR, "enroll", "--server", base,
                             "--code", code, "--allow-root", str(work)],
                            env=sidecar_env, stdout=logf, stderr=subprocess.STDOUT,
                            timeout=60)
    check("sidecar enroll 成功（接入码换出设备密钥）", enroll.returncode == 0,
          (tmp / "sidecar.log").read_text()[-300:])
    runner = subprocess.Popen([VENV_PY, SIDECAR, "run", "--wait", "5"],
                              env=sidecar_env, stdout=logf,
                              stderr=subprocess.STDOUT)
    sidecar["runner"] = runner
    online = None
    for _ in range(40):
        listed = call(base, "GET", "/computer-devices" + q, token=token)
        me = next((d for d in listed["devices"] if d["id"] == device_id), None)
        if me and me.get("online"):
            online = me
            break
        time.sleep(1)
    check("设备经隧道心跳上线并上报 4 项能力",
          bool(online) and len(online.get("capabilities") or []) == 4,
          str((online or {}).get("capabilities")))

    # ---- ① 远程读文件：直接放行，但真的在那台机器上跑 ----
    r = call(base, "POST", "/computer-agent/execute" + q,
             {"action_type": "read_file",
              "params": {"path": str(work / "seed.txt")},
              "device_id": device_id}, token)
    check("① 远程 read_file 成功且响应带设备名",
          r["result"]["ok"] and "设备上的原件内容" in "".join(r["result"].get("lines", []))
          and (r.get("device") or {}).get("name") == "验证用的 MacBook",
          json.dumps(r["result"], ensure_ascii=False)[:200])
    dsp = call(base, "GET", "/computer-devices/dispatches" + q, token=token)
    rows = dsp["dispatches"]
    check("① 走的是设备隧道（有派发记录且已成功回报）",
          rows and rows[0]["status"] == "succeeded"
          and rows[0]["device_id"] == device_id,
          f"{len(rows)} 条，最近：{rows[0]['capability']}/{rows[0]['status']}")

    # ---- ② 审批拦截 + 批准后在设备上落盘 ----
    notes = work / "notes.md"
    held = call(base, "POST", "/computer-agent/execute" + q,
                {"action_type": "write_file",
                 "params": {"path": str(notes), "content": "# 由员工在设备上写的\n"},
                 "device_id": device_id}, token)
    pending = call(base, "GET", f"/intervention/requests{q}&status=PENDING", token=token)
    req_id = pending[0]["id"] if pending else None
    payload = (pending[0].get("payload") or {}) if pending else {}
    check("② 远程 write_file 被审批门挂起（未落盘）",
          held.get("status") == "awaiting_approval" and bool(req_id)
          and not notes.exists(),
          f"mode={held.get('mode')} 设备={held.get('device')}")
    check("② 审批单冻结了目标设备且不演算本机 diff",
          payload.get("device_id") == device_id
          and payload.get("device_name") == "验证用的 MacBook"
          and (payload.get("preview") or {}).get("available") is False,
          json.dumps(payload.get("preview"), ensure_ascii=False)[:160])
    decided = call(base, "POST", f"/intervention/requests/{req_id}/decide" + q,
                   {"decision": "approve"}, token)
    exec_res = (decided or {}).get("execution") or {}
    check("② 批准后由 sidecar 真的写出文件",
          decided.get("status") == "APPROVED" and exec_res.get("ok")
          and notes.is_file() and "设备" in notes.read_text(encoding="utf-8"),
          json.dumps(exec_res, ensure_ascii=False)[:250])

    # ---- ③ 第二把锁：可写根目录之外，设备端自己拒 ----
    outside = tmp / "should-not-exist.txt"
    held2 = call(base, "POST", "/computer-agent/execute" + q,
                 {"action_type": "write_file",
                  "params": {"path": str(outside), "content": "越界\n"},
                  "device_id": device_id}, token)
    pending2 = call(base, "GET", f"/intervention/requests{q}&status=PENDING", token=token)
    decided2 = call(base, "POST",
                    f"/intervention/requests/{pending2[0]['id']}/decide" + q,
                    {"decision": "approve"}, token)
    res2 = ((decided2 or {}).get("execution") or {}).get("result") or {}
    check("③ 可写根目录之外被设备端护栏拒绝（后端批准也无效）",
          held2.get("status") == "awaiting_approval"
          and res2.get("ok") is False and res2.get("denied_locally") is True
          and not outside.exists(),
          str(res2.get("error"))[:160])

    # ---- ④ 设备不可用：绝不回落到后端本机 ----
    ghost = call(base, "POST", "/computer-devices" + q,
                 {"name": "从没接入过的 iMac"}, token)["device"]["id"]
    g = call(base, "POST", "/computer-agent/execute" + q,
             {"action_type": "read_file",
              "params": {"path": str(work / "seed.txt")},
              "device_id": ghost}, token)
    check("④ 未接入设备被如实拒绝，没有在本机偷读",
          g["result"]["ok"] is False and g["result"].get("device_unavailable")
          and "还没接入" in str(g["result"].get("error")),
          str(g["result"].get("error"))[:120])

    # ---- ⑤ 吊销后立刻领不到命令，且默认执行机被清掉 ----
    call(base, "POST", f"/computer-devices/{device_id}/set-default" + q, {}, token)
    rev = call(base, "POST", f"/computer-devices/{device_id}/revoke" + q, {}, token)
    after = call(base, "GET", "/computer-devices" + q, token=token)
    check("⑤ 吊销生效并清掉默认执行机",
          rev["revoked"] and after["default_device_id"] is None
          and next(d for d in after["devices"] if d["id"] == device_id)["revoked"],
          f"作废排队 {rev['cancelled_dispatches']} 条")
    sidecar_text = ""
    for _ in range(30):        # sidecar 正挂在长轮询上，下一轮领取才会撞到 401
        time.sleep(2)
        sidecar_text = (tmp / "sidecar.log").read_text()
        if "领取失败" in sidecar_text:
            break
    check("⑤ 被吊销的 sidecar 领不到命令（本机报密钥失效）",
          "领取失败" in sidecar_text,
          sidecar_text.strip().splitlines()[-1][:160] if sidecar_text else "无日志")

    # ---- ⑥ 审计里看得见「这次在哪台机器上跑的」 ----
    logs = call(base, "GET", f"/computer-agent/logs{q}&limit=20", token=token)
    remote = [l for l in logs if (l.get("result") or {}).get("device")]
    check("⑥ 审计日志带执行机器名", len(remote) >= 3,
          f"{len(remote)} 条远程记录 / 共 {len(logs)} 条")
    denied = [l for l in logs if not l["allowed"]]
    check("⑥ 拒绝与不可用也进了审计（不粉饰）", len(denied) >= 1,
          f"{len(denied)} 条 allowed=false")

    print("\n--- sidecar 日志（尾部）---")
    print("\n".join(sidecar_text.strip().splitlines()[-14:]))

    failed = [n for n, ok, _ in ROWS if not ok]
    print(f"\n结果：{len(ROWS) - len(failed)}/{len(ROWS)} 通过"
          + (f"，失败：{failed}" if failed else "，全绿"))
    runner.terminate()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
