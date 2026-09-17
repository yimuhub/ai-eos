"""P3-05 + P3-06 Career / Promotion / Transfer end-to-end test."""
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


print("=== P3-05/06 Career & Promotion ===")

# 0. 准备：技能树节点（复用 P3-04 的，若无则建）
tree = api("GET", "/skill-growth/tree", {"company_id": CID})
nodes = {t["key"]: t["id"] for t in tree}
if "basic_research" not in nodes:
    n = api("POST", "/skill-growth/nodes", {}, {"key": "basic_research", "name": "基础市场研究"})
    nodes["basic_research"] = n["id"]

# 1. 建职位路径：市场研究员(L1) → 高级市场研究员(L2) → 项目经理(L2, 转岗)
p_junior = api("POST", "/career/positions", {"company_id": CID},
               {"title": "市场研究员", "level": 1, "department": "市场部"})
p_senior = api("POST", "/career/positions", {"company_id": CID},
               {"title": "高级市场研究员", "level": 2, "department": "市场部",
                "required_skill_node_ids": [nodes["basic_research"]]})
p_pm = api("POST", "/career/positions", {"company_id": CID},
           {"title": "项目经理", "level": 2, "department": "项目中心",
            "required_skill_node_ids": [nodes["basic_research"], "nonexistent-skill"]})
check("职位路径建立", all([p_junior.get("id"), p_senior.get("id"), p_pm.get("id")]))

# 2. 入职
r = api("POST", f"/career/agents/{MKT}/join?position_id={p_junior['id']}")
check("入职市场研究员", r.get("record_id"))
# 重复入职被拒
r2 = api("POST", f"/career/agents/{MKT}/join?position_id={p_senior['id']}")
check("重复入职被拒", r2.get("__http_error__") == 409)

# 3. Gap 分析（转岗项目经理：缺 nonexistent-skill）
gap = api("GET", f"/career/agents/{MKT}/gap-analysis/{p_pm['id']}")
check("Gap分析发现缺口", len(gap.get("missing_skills", [])) >= 1
      and not gap.get("ready_for_trial"))
print(f"     缺口: {gap.get('missing_skills')} | 训练计划: "
      f"{len(gap.get('training_plan', []))} 步")

# 4. 未过试岗就提案晋升 → 批准后仍可应用（人工决策权），但先走完整流程：
prop = api("POST", f"/career/agents/{MKT}/proposals", {},
           {"proposal_type": "PROMOTION", "to_position_id": p_senior["id"],
            "reason": "完成多个市场任务，表现优秀"})
check("晋升提案创建(含Gap快照)", prop.get("status") == "PROPOSED")
pid = prop["id"]

# 拒绝第一次 → 不生效
rr = api("POST", f"/career/proposals/{pid}/decide", {}, {"decision": "reject"})
check("提案拒绝不生效", rr.get("status") == "REJECTED")

# 再提一次并批准 → 应用
prop2 = api("POST", f"/career/agents/{MKT}/proposals", {},
            {"proposal_type": "PROMOTION", "to_position_id": p_senior["id"],
             "reason": "技能已达标"})
ra = api("POST", f"/career/proposals/{prop2['id']}/decide", {},
         {"decision": "approve", "note": "绩效优秀，批准晋升"})
check("批准晋升生效", ra.get("status") == "APPLIED"
      and ra.get("new_position") == "高级市场研究员")

# 5. 履历时间线：入职 + 晋升两条记录
tl = api("GET", f"/career/agents/{MKT}/timeline")
check("履历时间线(2条)", len(tl) == 2 and tl[-1]["is_current"] is True)
check("旧职位自动结束", tl[0]["is_current"] is False)

# 6. 成长时间线包含 PROMOTED
prof = api("GET", f"/learning/agents/{MKT}/profile")
types = [e["type"] for e in prof["growth_events"]]
check("成长事件 PROMOTED 记录", "PROMOTED" in types)

print(f"\n=== P3-05/06 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
