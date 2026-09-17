"""P3-12 Advanced Automation end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"
MKT = "4ef2b1cd-d607-48d3-9ded-8244ce0b012d"

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def api(method, path, params=None, data=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=== P3-12 Advanced Automation ===")

# 1. 创建多步自动化（TEST 模式 = DryRun）
r = api("POST", "/automations", {"company_id": CID},
        {"name": "每日行业监测-简报流程",
         "trigger_type": "TIME",
         "mode": "TEST",
         "steps": [
             {"seq": 1, "type": "send_message",
              "params": {"sender_agent_id": MKT, "message_type": "PROGRESS",
                         "title": "行业监测完成", "content": "发现3条重要信息"}},
             {"seq": 2, "type": "create_task",
              "params": {"title": "分析今日行业动态", "assigned_agent_id": CEO}},
             {"seq": 3, "type": "wait_approval"},
         ]})
check("创建自动化(TEST)", r.get("id"))
aid = r["id"]

# 2. Dry Run 执行：全部模拟，无副作用
run = api("POST", f"/automations/{aid}/run")
check("DryRun 模拟执行", run.get("status") == "WAITING_APPROVAL")
simulated = [s for s in (run.get("steps") or [])
             if (s.get("result") or {}).get("simulated")]
check("前两步为模拟(无副作用)", len(simulated) == 2)
check("第三步等待审批", any(s.get("status") == "WAITING_APPROVAL"
                          for s in run.get("steps") or []))

# 3. 审批继续
rid = run.get("run_id")
ra = api("POST", f"/automations/runs/{rid}/approve-continue")
check("审批继续", ra.get("status") == "SUCCESS")

# 4. PRODUCTION 模式真实执行（send_message + create_task 真实落库）
api("PUT" if False else "POST", f"/automations/{aid}/enable")
d_list = api("GET", "/automations", {"company_id": CID})
# 直接建一个 PRODUCTION 版本跑
rp = api("POST", "/automations", {"company_id": CID},
         {"name": "生产版-自动派单",
          "trigger_type": "EVENT",
          "mode": "PRODUCTION",
          "steps": [
              {"seq": 1, "type": "create_task",
               "params": {"title": "[自动化] 行业周报任务",
                          "assigned_agent_id": MKT}},
              {"seq": 2, "type": "send_message",
               "params": {"sender_agent_id": CEO, "message_type": "TASK_ASSIGNED",
                          "title": "新任务已派发", "content": "请查看任务列表"}},
          ]})
aid_prod = rp["id"]
run_p = api("POST", f"/automations/{aid_prod}/run")
check("PRODUCTION 真实执行", run_p.get("status") == "SUCCESS", str(run_p)[:120])
real_steps = [s for s in (run_p.get("steps") or []) if s.get("status") == "SUCCESS"
              and not s.get("simulated")]
check("两步均真实落库", len(real_steps) == 2 and
      all("task_id" in s.get("result", {}) or "message_id" in s.get("result", {})
          for s in real_steps))

# 5. 失败重试语义：未知步骤类型 → FAILED 且中止
rf = api("POST", "/automations", {"company_id": CID},
         {"name": "失败测试", "trigger_type": "MANUAL", "mode": "PRODUCTION",
          "steps": [{"seq": 1, "type": "unknown_type", "params": {}}]})
run_f = api("POST", f"/automations/{rf['id']}/run")
check("未知步骤→FAILED", run_f.get("status") == "FAILED")

# 6. 紧急停止
es = api("POST", "/automations/emergency-stop?company_id=" + CID)
check("紧急停止接口", "stopped_runs" in es)

print(f"\n=== P3-12 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
