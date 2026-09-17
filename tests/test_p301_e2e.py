"""P3-01 Agent Learning Center end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
MKT = "4ef2b1cd-d607-48d3-9ded-8244ce0b012d"  # 市场研究员

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
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=== P3-01 Agent Learning Center ===")

# 1. 首次访问自动建档
p1 = api("GET", f"/learning/agents/{MKT}/profile")
check("自动建档", p1["profile"]["version"] == 1 and p1["profile"]["identity"])
ev_types = [e["type"] for e in p1["growth_events"]]
check("建档 CREATED 事件", "CREATED" in ev_types)

# 2. 更新能力 → 版本自增 + 能力历史 + CAPABILITY_CHANGED 事件
r = api("PATCH", f"/learning/agents/{MKT}/profile",
        {"company_id": CID},
        {"capabilities": ["市场研究", "竞品分析"],
         "preferences": {"analysis_method": "SWOT"}})
check("档案更新 v→2", r.get("changed") and r.get("version") == 2)

p2 = api("GET", f"/learning/agents/{MKT}/profile")
check("能力历史版本化", len(p2["capability_history"]) == 1)
check("偏好已合并存储",
      p2["profile"]["preferences"].get("analysis_method") == "SWOT")

# 3. 再次更新能力 → 第二个能力版本（禁止覆盖历史）
api("PATCH", f"/learning/agents/{MKT}/profile", {"company_id": CID},
    {"capabilities": ["市场研究", "竞品分析", "数据分析"]})
p3 = api("GET", f"/learning/agents/{MKT}/profile")
caps_versions = [c["capabilities"] for c in p3["capability_history"]]
check("历史不可覆盖(两个版本)", len(caps_versions) == 2 and
      caps_versions[0] != caps_versions[1])

# 4. 学习计划
rec = api("POST", f"/learning/agents/{MKT}/learning-records",
          {"company_id": CID},
          {"goal": "掌握文旅行业政策研究方法"})
check("创建学习目标", rec.get("status") == "PLANNED")
r = api("PATCH", f"/learning/learning-records/{rec['id']}",
        {}, {"progress": 100})
check("学习进度100%自动DONE", r.get("status") == "DONE")

# 5. 成长事件时间线完整可追溯
events = api("GET", f"/learning/agents/{MKT}/growth-events")
types = [e["type"] for e in events]
check("成长时间线含全部关键事件",
      "CREATED" in types and "CAPABILITY_CHANGED" in types
      and "TRAINED" in types)
created_times = [e["created_at"] for e in events]
check("事件按时间倒序", created_times == sorted(created_times, reverse=True))

print(f"\n=== P3-01 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
