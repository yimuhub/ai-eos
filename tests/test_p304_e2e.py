"""P3-04 Skill Growth end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
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
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=== P3-04 Skill Growth ===")

# 1. 建技能树：市场分析 → 竞品研究（前置：基础市场研究）
n_root = api("POST", "/skill-growth/nodes", {},
             {"key": "market_analysis", "name": "市场分析"})
n_base = api("POST", "/skill-growth/nodes", {},
             {"key": "basic_research", "name": "基础市场研究", "parent_id": n_root["id"]})
n_comp = api("POST", "/skill-growth/nodes", {},
             {"key": "competitor_research", "name": "竞品研究",
              "parent_id": n_root["id"], "prerequisite_id": n_base["id"]})
check("技能树建立(3节点)", all([n_root.get("id"), n_base.get("id"), n_comp.get("id")]))

tree = api("GET", "/skill-growth/tree", {"company_id": CID})
check("树可查询", len([t for t in tree if t["key"].startswith(("market", "basic", "competitor"))]) >= 3)

# 2. 前置技能拦截：直接学竞品研究应被拒
r = api("POST", f"/skill-growth/agents/{MKT}/acquire", {},
        {"skill_node_id": n_comp["id"]})
check("前置技能拦截", r.get("__http_error__") == 409)

# 3. 先学基础，再学竞品
r = api("POST", f"/skill-growth/agents/{MKT}/acquire", {},
        {"skill_node_id": n_base["id"],
         "evidence": [{"type": "training", "note": "P3-02 训练获得"}]})
check("获得基础技能 BEGINNER", r.get("level") == "BEGINNER")
asn_base = r["id"]
r = api("POST", f"/skill-growth/agents/{MKT}/acquire", {},
        {"skill_node_id": n_comp["id"]})
check("满足前置后可学竞品", r.get("level") == "BEGINNER")
asn_comp = r["id"]

# 4. 无证据升级被拒（禁止虚假声称）
r = api("POST", f"/skill-growth/agent-skills/{asn_base}/upgrade", {},
        {"new_level": "JUNIOR", "method": "user_confirm"})
check("无证据升级被拒", r.get("__http_error__") == 409, str(r)[:80])

# 5. 补证据后升级成功（BEGINNER→JUNIOR 需 2 条）
for i in range(2):
    api("POST", f"/skill-growth/agent-skills/{asn_base}/evidence", {},
        {"evidence_type": "task", "ref_id": f"task-{i}",
         "note": f"完成市场研究任务{i+1}"})
r = api("POST", f"/skill-growth/agent-skills/{asn_base}/upgrade", {},
        {"new_level": "JUNIOR", "score": 78, "method": "task_review",
         "evidence_summary": "两个真实任务通过"})
check("证据充分后升级 JUNIOR", r.get("level") == "JUNIOR", str(r)[:100])

# 6. 跨级升级被拒
r = api("POST", f"/skill-growth/agent-skills/{asn_base}/upgrade", {},
        {"new_level": "EXPERT"})
check("跨级升级被拒", r.get("__http_error__") == 409)

# 7. 回滚
r = api("POST", f"/skill-growth/agent-skills/{asn_base}/rollback/BEGINNER")
check("回滚到 BEGINNER", r.get("level") == "BEGINNER")

# 8. Skill Matrix（要求：使用过的技能必须有证据；未用的可为0）
mx = api("GET", f"/skill-growth/agents/{MKT}/matrix")
rows = {m["skill"]: m for m in mx if m["skill"] in ("基础市场研究", "竞品研究")}
check("Skill Matrix 输出", len(rows) == 2)
base = rows.get("基础市场研究", {})
check("使用过的技能有证据", base.get("evidence_count", 0) >= 1 and
      base.get("experience_count", 0) >= 1)

print(f"\n=== P3-04 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
