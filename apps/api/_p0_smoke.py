"""V5.5 P0 冒烟：晨间简报 + 高危操作审批门 + 预算上限。
进程内 httpx ASGITransport（避开 shell 密码脱敏）。
"""
import asyncio, httpx, os, sys, time

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("AIEOS_DEV_SKIP_LLM_IMPORT", "1")

from app.main import app  # noqa: E402

BASE = "http://aieos.local"
UNAME = f"p0_smoke_{int(time.time())}"
PASSWD = "SmokePass#1"


async def main():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=60) as c:
        passed, failed = 0, []

        def check(name, cond, extra=""):
            nonlocal passed
            if cond:
                passed += 1
                print(f"  ✓ {name}")
            else:
                failed.append(name)
                print(f"  ✗ {name} {extra}")

        # ---- 0) 建临时用户 + 登录 ----
        r = await c.post("/auth/register", json={
            "email": f"{UNAME}@aieos-smoke.com", "username": UNAME,
            "password": PASSWD, "display_name": "P0 Smoke"})
        r = await c.post("/auth/login", json={"identity": UNAME, "password": PASSWD})
        check("登录 200", r.status_code == 200, f"got {r.status_code} {r.text[:80]}")
        H = {"Authorization": f"Bearer {r.json().get('access_token', '')}"}

        # ---- 1) 建公司（简报/审批门/预算共用）----
        r = await c.post("/companies", json={"name": f"P0 Smoke {UNAME}"}, headers=H)
        check("建公司 201", r.status_code in (200, 201), f"got {r.status_code} {r.text[:100]}")
        company_id = r.json().get("id", "")

        # ---- 2) CEO 晨间简报 ----
        r = await c.get("/briefing")
        check("GET /briefing 401 无 token", r.status_code == 401)
        r = await c.get("/briefing", params={"company_id": company_id}, headers=H)
        check("GET /briefing 200", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
        b = r.json()
        check("简报含四大块", all(k in b for k in ("greeting", "yesterday", "today", "needs_you")),
              f"keys={list(b.keys())}")
        check("简报含 cost_today", "cost_today" in b)

        # ---- 3) 高危操作审批门 ----
        # 开审批开关
        r = await c.put("/intervention/policy",
                        json={"level": 2, "approval_actions": ["computer_command"]},
                        params={"company_id": company_id}, headers=H)
        check("策略 PUT 200", r.status_code == 200, f"got {r.status_code} {r.text[:100]}")

        # 先授权 run_shell 能力（审批门之上还有能力授权双层防线）
        r = await c.post("/computer-agent/grant",
                         params={"capability": "run_shell", "company_id": company_id},
                         headers=H)
        check("授权 run_shell", r.status_code in (200, 201), f"got {r.status_code} {r.text[:100]}")

        # 高危操作应被拦截
        r = await c.post("/computer-agent/execute",
                         json={"action_type": "run_shell",
                               "params": {"command": "echo p0_gate_ok"}},
                         params={"company_id": company_id}, headers=H)
        check("高危 run_shell 被拦截 awaiting_approval",
              r.status_code == 200 and r.json().get("status") == "awaiting_approval",
              f"got {r.status_code} {r.text[:150]}")
        approval_id = r.json().get("approval_request_id", "")
        check("返回审批单 id", bool(approval_id))

        # 审批单进队列
        r = await c.get("/intervention/requests",
                        params={"company_id": company_id, "status": "PENDING"}, headers=H)
        check("审批队列有 1 条 PENDING",
              r.status_code == 200 and len([x for x in r.json() if x.get("action_type") == "computer_command"]) == 1,
              f"got {r.text[:150]}")

        # 批准 → 自动补执行
        r = await c.post(f"/intervention/requests/{approval_id}/decide",
                         json={"decision": "approve", "note": "P0 smoke"}, headers=H)
        check("批准 200", r.status_code == 200, f"got {r.status_code} {r.text[:150]}")
        ex = r.json().get("execution", {})
        check("批准后自动执行了命令", ex.get("ok") is True, f"execution={ex}")

        # 非高危操作（screenshot）不受审批门影响
        r = await c.post("/computer-agent/execute",
                         json={"action_type": "screenshot", "params": {}},
                         params={"company_id": company_id}, headers=H)
        check("screenshot 不是 awaiting_approval",
              r.json().get("status") != "awaiting_approval", f"{r.text[:120]}")

        # ---- 4) 预算上限 ----
        r = await c.post("/token-budgets",
                         json={"scope_type": "company", "limit_tokens": 1000,
                               "period": "day"},
                         params={"company_id": company_id}, headers=H)
        check("建预算 201/200", r.status_code in (200, 201), f"got {r.status_code} {r.text[:120]}")
        budget = r.json()
        bid = budget.get("id", "")
        check("预算初始未挂起", budget.get("paused") in (False, None))

        # 造一条今天的用量流水（超 1000 token）
        from app.db.session import engine
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text(
                "INSERT INTO token_usages (id, company_id, total_tokens, total_cost, "
                "input_tokens, output_tokens, input_cost, output_cost, model_id, "
                "scenario, latency_ms, success, created_at) VALUES "
                "(:id, :cid, 5000, 3.5, 3000, 2000, 2.5, 1.0, 'smoke-model', 'TASK', 0, TRUE, NOW())"),
                               {"id": str(__import__("uuid").uuid4()), "cid": company_id})
            await conn.commit()

        r = await c.get("/token-budgets", params={"company_id": company_id}, headers=H)
        bl = r.json().get("budgets", [])
        hit = [x for x in bl if x.get("id") == bid]
        check("用量超上限 → exceeded=True", hit and hit[0].get("exceeded") is True,
              f"budgets={bl[:2]}")
        check("超上限自动挂起 paused=True", hit and hit[0].get("paused") is True,
              f"paused={hit[0].get('paused') if hit else None} "
              f"reason={hit[0].get('paused_reason') if hit else None}")

        # 老板调高 → 立即恢复
        r = await c.put(f"/token-budgets/{bid}",
                        json={"limit_tokens": 100000},
                        params={"company_id": company_id}, headers=H)
        check("调高上限 200", r.status_code == 200, f"got {r.status_code}")
        check("调高后 paused 复位", r.json().get("paused") is False,
              f"paused={r.json().get('paused')}")

        print(f"\n[结果] {passed} 通过, {len(failed)} 失败 {failed if failed else ''}")
        return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
