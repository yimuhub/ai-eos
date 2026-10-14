"""P3-08 Agent Factory 2.0 end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()

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
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:300]}


print("=== P3-08 Agent Factory 2.0 ===")

# 1. 根据目标生成蓝图（LLM）
r = api("POST", "/factory/blueprint", {"company_id": CID},
        {"goal": "成立短视频新媒体运营团队，负责文旅项目的抖音/小红书内容策划与投放"})
check("蓝图生成(LLM)", r.get("status") == "BLUEPRINT" and
      (r.get("blueprint") or {}).get("name"))
bp = r.get("blueprint") or {}
job = r.get("job_id")
print(f"     蓝图: {bp.get('name')} / {bp.get('role')} | "
      f"职责{len(bp.get('responsibilities', []))}项 技能{len(bp.get('required_skills', []))}项")

# 2. 用户编辑蓝图后批准 → 创建真实 Agent（TESTING）
edited = dict(bp)
edited["name"] = "短视频运营专员"
ra = api("POST", f"/factory/jobs/{job}/approve",
         {"company_id": CID}, {"blueprint": edited})
check("批准并创建Agent(TESTING)", ra.get("status") == "TESTING"
      and ra.get("agent_name") == "短视频运营专员")
agent_id = ra.get("agent_id")

# 3. Agent 处于测试期（DISABLED），不能立即上岗
agents = api("GET", "/agents", {"company_id": CID})
created = [x for x in agents if x["id"] == agent_id]
check("新Agent测试期 DISABLED", created and created[0]["status"] == "DISABLED")

# 4. 测试不通过 → FAILED + DISABLED（先验证失败分支）
rb = api("POST", f"/factory/jobs/{job}/test-result",
         {"passed": False, "notes": "文案质量差"})
check("测试不通过→FAILED", rb.get("status") == "FAILED")

# 5. 再走一次完整流程：蓝图→批准→测试通过→ACTIVE
r2 = api("POST", "/factory/blueprint", {"company_id": CID},
         {"goal": "需要一名数据分析师，负责项目数据看板与周报"})
job2 = r2.get("job_id")
ra2 = api("POST", f"/factory/jobs/{job2}/approve",
          {"company_id": CID}, {})
check("第二个Agent创建", ra2.get("status") == "TESTING")
rp = api("POST", f"/factory/jobs/{job2}/test-result",
         {"passed": True, "score": 88, "notes": "看板搭建正确"})
check("测试通过→ACTIVE", rp.get("status") == "ACTIVE")

agents = api("GET", "/agents", {"company_id": CID})
a2 = [x for x in agents if x["id"] == ra2.get("agent_id")]
check("通过后 Agent IDLE 上岗", a2 and a2[0]["status"] == "IDLE")

jobs = api("GET", "/factory/jobs", {"company_id": CID})
print(f"     Factory 任务记录: {len(jobs)} 条 "
      f"({sum(1 for j in jobs if j['status']=='ACTIVE')} ACTIVE)")

print(f"\n=== P3-08 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
