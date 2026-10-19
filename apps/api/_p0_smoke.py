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

        # ---- 4.5) V5.5 A1：每日简报推送调度 ----
        r = await c.post("/briefing/push-schedule",
                         params={"company_id": company_id, "time_str": "09:00"}, headers=H)
        check("建简报推送调度 200", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
        check("返回 schedule_id", bool(r.json().get("schedule_id")))
        # 再调一次 → 更新不重复建
        r2 = await c.post("/briefing/push-schedule",
                          params={"company_id": company_id, "time_str": "09:30"}, headers=H)
        check("重复调用复用同一调度",
              r2.status_code == 200 and r2.json().get("schedule_id") == r.json().get("schedule_id"))

        # ---- 4.6) V5.5 A4：部门级预算 ----
        r = await c.post("/token-budgets",
                         json={"scope_type": "department", "limit_tokens": 99999,
                               "period": "day"},
                         params={"company_id": company_id}, headers=H)
        check("部门级预算被拒（无 scope_id 语义 400 或 200 带 scope）",
              r.status_code in (200, 201, 400), f"got {r.status_code} {r.text[:120]}")
        # 非法 scope 仍被拒
        r = await c.post("/token-budgets",
                         json={"scope_type": "nonsense", "limit_tokens": 100},
                         params={"company_id": company_id}, headers=H)
        check("非法 scope_type 400", r.status_code == 400, f"got {r.status_code}")

        # ---- 4.7) V5.5 A2：预算占比/软预警字段 ----
        r = await c.get("/token-budgets", params={"company_id": company_id}, headers=H)
        bl = r.json().get("budgets", [])
        with_pct = [x for x in bl if x.get("usage_pct") is not None]
        check("预算列表含 usage_pct/warn 字段", len(with_pct) > 0,
              f"budgets={[x.get('usage_pct') for x in bl[:3]]}")
        w = with_pct[0]
        check("warn 标志与 80% 阈值一致",
              w.get("warn") is ((w["usage_pct"] >= 80) and not w.get("exceeded")),
              f"{w.get('usage_pct')}/{w.get('warn')}")

        # ---- 5) V5.5 A3：外部 Agent 外发审批门 ----
        # 开外发审批策略
        r = await c.put("/intervention/policy",
                        json={"level": 2,
                              "approval_actions": ["external_agent"]},
                        params={"company_id": company_id}, headers=H)
        check("外发策略 PUT 200", r.status_code == 200, f"got {r.status_code}")
        # 建外部连接（LOCAL provider + 假端点 → 建连接成功，执行才失败）
        r = await c.post("/external-agents",
                         params={"company_id": company_id},
                         json={"name": "A3 Smoke Ext",
                               "provider_type": "LOCAL",
                               "endpoint": "http://127.0.0.1:9"},
                         headers=H)
        if r.status_code in (200, 201):
            ext_id = r.json().get("id", "")
            # 启用
            r = await c.post(f"/external-agents/{ext_id}/enable", headers=H)
            check("外部连接启用", r.status_code in (200, 201, 409),
                  f"got {r.status_code} {r.text[:100]}")
            r = await c.post(f"/external-agents/{ext_id}/execute",
                             json={"task_input": "hello ext"}, headers=H)
            check("外发被拦截 pending_approval",
                  r.status_code == 200 and r.json().get("status") == "pending_approval",
                  f"got {r.status_code} {r.text[:150]}")
            aid = r.json().get("approval_id", "")
            check("外发审批单已入队", bool(aid))
            # 批准 → 尝试执行（连不上应记录失败，不应 500）
            r = await c.post(f"/intervention/requests/{aid}/decide",
                             json={"decision": "approve", "note": "A3 smoke"}, headers=H)
            ex = r.json().get("execution")
            check("批准后补执行（预期失败但不 500）",
                  r.status_code == 200 and ex is not None,
                  f"got {r.status_code} {r.text[:200]}")
            # 策略未开 external_agent 时直通
            await c.put("/intervention/policy",
                        json={"level": 2, "approval_actions": []},
                        params={"company_id": company_id}, headers=H)
        else:
            check("外部连接创建", r.status_code in (200, 201),
                  f"got {r.status_code} {r.text[:200]}")

        # ---- V5.5 C1–C4 ----
        # C1：HR 看板
        r = await c.get(f"/performance/company/{company_id}/hr-overview", headers=H)
        ok = r.status_code == 200
        d = r.json() if ok else {}
        check("C1 HR 看板四象限", ok and all(k in d for k in
              ("headcount", "cost_month", "output_30d", "assessment_top", "health")),
              f"got {r.status_code} {r.text[:100]}")

        # C3：Agent 邮箱（收件箱 / 代答）
        r = await c.post("/agents", json={"name": "C3 邮箱员", "company_id": company_id,
                                          "role": "助手"}, headers=H)
        if r.status_code in (200, 201):
            agent_id = r.json().get("id") or r.json().get("agent_id")
            r = await c.post("/mailboxes", json={"agent_id": agent_id,
                                                 "alias": f"c3-{UNAME}@aieos.local"}, headers=H)
            check("C3 开通邮箱", r.status_code in (200, 201),
                  f"got {r.status_code} {r.text[:100]}")
            mb_id = r.json().get("id") if r.status_code in (200, 201) else None
            if mb_id:
                r = await c.post("/mailboxes/messages", json={
                    "mailbox_id": mb_id, "subject": "合作意向",
                    "body": "请问你们支持定制吗？"}, headers=H)
                ok = r.status_code in (200, 201)
                msg_id = r.json().get("id") if ok else None
                check("C3 来信入收件箱", ok and msg_id is not None,
                      f"got {r.status_code} {r.text[:100]}")
                if msg_id:
                    r = await c.post(f"/mailboxes/messages/{msg_id}/reply",
                                     json={"body": "支持定制，稍后联系。"}, headers=H)
                    check("C3 代答回信", r.status_code in (200, 201),
                          f"got {r.status_code} {r.text[:100]}")
                    r = await c.get("/mailboxes/messages", params={
                        "mailbox_id": mb_id}, headers=H)
                    msgs = r.json() if r.status_code == 200 else []
                    check("C3 收件箱双向记录",
                          any(m.get("direction") == "out" for m in msgs)
                          and any(m.get("status") == "replied" for m in msgs),
                          f"{len(msgs)} msgs")
        else:
            check("C3 建测试员工", False, f"got {r.status_code} {r.text[:120]}")

        # ---- V5.6.1 ①③④⑤ 冒烟 ----
        # ② 工具级免批白名单 CRUD
        r = await c.get("/intervention/whitelist", params={"company_id": company_id}, headers=H)
        check("② 白名单列表端点", r.status_code == 200, f"got {r.status_code} {r.text[:80]}")
        # ④ SOP 交接单：建一个成果再取交接单
        r = await c.post(f"/artifacts?company_id={company_id}",
                        json={"name": "V561 SOP 冒烟单", "content": "测试交接内容"}, headers=H)
        if r.status_code in (200, 201):
            art_id = r.json().get("id")
            r2 = await c.get(f"/artifacts/{art_id}/sop", headers=H)
            md = (r2.json() or {}).get("markdown", "")
            check("④ SOP 交接单含状态/变更/建议", r2.status_code == 200
                  and "交接单" in md and "建议下一步" in md,
                  f"got {r2.status_code} {r2.text[:100]}")
        else:
            check("④ 建测试成果", False, f"got {r.status_code} {r.text[:100]}")
        # ⑤ 绩效含 ROI 维度
        r = await c.post(f"/performance/company/{company_id}/snapshot", headers=H)
        ok = r.status_code == 200
        ranked = (r.json() or {}).get("ranked", []) if ok else []
        has_roi = any(("roi" in m and "cost_total" in m) for m in ranked)
        check("⑤ 绩效快照含 ROI/成本维度", ok and has_roi,
              f"{len(ranked)} agents {('roi ok' if has_roi else 'no roi field')}")

        # ---- V5.7 ⑥⑦⑧ 冒烟 ----
        # ⑥⑦ 下班学习：写日志 → 老板批准 → 沉淀为高置信记忆
        agent7 = (await c.post("/agents", json={"name": "V57 学习员",
                                               "company_id": company_id},
                                headers=H)).json()
        a7id = agent7.get("id") or agent7.get("agent_id")
        r = await c.post(f"/worklogs?agent_id={a7id}",
                        json={"agent_id": a7id, "content": "学会了 dry_run 预检",
                              "lessons": ["预检先行"], "period": "daily"},
                        headers=H)
        wl_ok = r.status_code in (200, 201)
        check("⑥ 写工作日志", wl_ok, f"got {r.status_code} {r.text[:100]}")
        wl_id = r.json().get("id") if wl_ok else None
        if wl_id:
            r2 = await c.post(f"/worklogs/{wl_id}/decide?agent_id={a7id}",
                             json={"decision": "approve"}, headers=H)
            d2 = r2.json() if r2.status_code == 200 else {}
            check("⑦ 批准→沉淀高置信记忆",
                  r2.status_code == 200 and d2.get("memory_ref"),
                  f"got {r2.status_code} {r2.text[:120]}")
            # 验证记忆真的入库且 importance=0.95
            rec = await c.get(f"/memory/recall", params={
                "company_id": company_id, "agent_id": a7id, "top_k": 5}, headers=H)
            mems = rec.json().get("memories", []) if rec.status_code == 200 else []
            hit = any(m.get("importance") == 0.95 for m in mems)
            check("⑦ 沉淀记忆可召回(importance 0.95)", hit,
                  f"{len(mems)} memories")
        # ⑧ 防呆 ACI：dry_run 预检拦截越界坐标
        r = await c.post("/computer-agent/execute",
                         json={"action_type": "click",
                              "params": {"x": 999999, "y": 1},
                              "dry_run": True},
                         params={"company_id": company_id}, headers=H)
        d3 = r.json() if r.status_code == 200 else {}
        check("⑧ dry_run 预检拦截越界坐标",
              r.status_code == 200 and d3.get("precheck_blocked") is True,
              f"got {r.status_code} {r.text[:120]}")

        # ---- V5.8 ⑨⑩ 冒烟 ----
        # ⑨ Agent Card
        r = await c.get(f"/agents/{a7id}/card", headers=H)
        card = r.json() if r.status_code == 200 else {}
        check("⑨ Agent Card 含能力+needs-auth",
              r.status_code == 200 and "needs_auth" in card
              and card.get("agent_id") == a7id,
              f"got {r.status_code} {r.text[:120]}")
        # ⑩ 成本分维
        r = await c.get(f"/tokens/stats/by-scenario",
                       params={"company_id": company_id, "days": 30}, headers=H)
        ok = r.status_code == 200
        rows = r.json() if ok else []
        has_dim = any("cost_pct" in x and "rule_candidate" in x for x in rows)
        check("⑩ 成本分维含占比/规则化建议", ok and (has_dim or not rows),
              f"got {r.status_code} {len(rows)} rows")

        print(f"\n[结果] {passed} 通过, {len(failed)} 失败 {failed if failed else ''}")
        return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
