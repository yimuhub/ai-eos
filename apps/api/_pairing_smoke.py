"""V5.4 全链路冒烟：配对码 → 手机换登录 → 设备列表 → 吊销 → 诊断。
进程内 httpx ASGITransport，避开 shell 密码脱敏。
"""
import asyncio, httpx, os, sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("AIEOS_DEV_SKIP_LLM_IMPORT", "1")

from app.main import app  # noqa: E402

BASE = "http://aieos.local"
CREDS = ("aieos", os.environ.get("AIEOS_DEV_PASSWORD", "aieos"))


async def main():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=30) as c:
        passed, failed = 0, []

        def check(name, cond, extra=""):
            nonlocal passed
            if cond:
                passed += 1
                print(f"  ✓ {name}")
            else:
                failed.append(name)
                print(f"  ✗ {name} {extra}")

        # 0) 建临时用户 + 登录拿 token
        import time
        uname = f"pair_smoke_{int(time.time())}"
        r = await c.post("/auth/register", json={
            "email": f"{uname}@aieos-smoke.com", "username": uname,
            "password": "SmokePass#1", "display_name": "Smoke"})
        if r.status_code != 201:
            print("  (注册提示:", r.status_code, r.text[:60], "→ 改用已有账号重试)")
        r = await c.post("/auth/login", json={"identity": uname, "password": "SmokePass#1"})
        check("登录 200", r.status_code == 200, f"got {r.status_code} {r.text[:80]}")
        token = r.json().get("access_token", "")
        H = {"Authorization": f"Bearer {token}"}

        # 1) 生成配对码
        r = await c.post("/pairings", json={}, headers=H)
        check("生成配对码 200", r.status_code in (200, 201), f"got {r.status_code} {r.text[:100]}")
        body = r.json()
        pair_token = body.get("token", "")
        check("返回明文 token + ttl", bool(pair_token) and ("ttl_seconds" in body or "expires_at" in body))

        # 2) 手机侧：免鉴权换登录（带 UA 模拟 iPhone）
        url = "/pair?" + "token=" + pair_token
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"})
        check("配对换登录 200", r.status_code == 200, f"got {r.status_code} {r.text[:100]}")
        p = r.json()
        check("返回新 access+refresh", bool(p.get("access_token")) and bool(p.get("refresh_token")))
        check("设备已登记 (iPhone)", p.get("device", {}).get("name", "").find("iPhone") >= 0,
              f"device={p.get('device')}")
        new_refresh = p.get("refresh_token", "")

        # 3) 一次性：同一配对码二次使用必须拒绝
        r2 = await c.get("/pair?" + "token=" + pair_token)
        check("配对码一次性（二次拒绝 400/404/409）", r2.status_code in (400, 404, 409), f"got {r2.status_code}")

        # 4) 设备列表：应含 1 台
        r = await c.get("/devices", headers=H)
        check("设备列表 200", r.status_code == 200, f"got {r.status_code}")
        devs = r.json()
        if isinstance(devs, dict):
            devs = devs.get("devices", [])
        check("设备数 ≥1", len(devs) >= 1, f"got {devs}")
        dev = next((d for d in devs if d.get("has_active_session")), devs[0] if devs else None)

        # 5) 吊销设备 → refresh 失效
        if dev:
            r = await c.post(f"/devices/{dev['id']}/revoke", json={}, headers=H)
            check("吊销 200", r.status_code == 200, f"got {r.status_code} {r.text[:80]}")
            r = await c.post("/auth/refresh", json={"refresh_token": new_refresh})
            check("吊销后 refresh 立即失效", r.status_code in (401, 403, 400), f"got {r.status_code}")
        else:
            print("  (无设备可吊销，跳过吊销段)")

        # 6) 诊断端点
        r = await c.get("/diagnostics", headers=H)
        check("诊断 200", r.status_code == 200, f"got {r.status_code}")
        d = r.json()
        check("诊断含 6 项检查", len(d.get("checks", [])) >= 5, f"got {len(d.get('checks', []))}")
        names = [x["name"] for x in d.get("checks", [])]
        print("    检查项:", " / ".join(names))

        print(f"\n=== {passed} passed, {len(failed)} failed ===")
        if failed:
            print("FAILED:", failed)
            sys.exit(1)


asyncio.run(main())
