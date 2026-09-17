"""P3-03 Experience & Reflection Engine end-to-end test."""
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:300]}


print("=== P3-03 Experience & Reflection Engine ===")

# 1. 记录一次 PARTIAL 工作经历并触发 LLM 复盘（对应任务书示例场景）
r = api("POST", "/reflection/experiences",
        {"agent_id": MKT},
        {"agent_id": MKT,
         "title": "完成文旅项目汇报",
         "outcome": "PARTIAL",
         "summary": "汇报结构完整，但政策引用部分被指出不够准确",
         "user_feedback": "结构不错；但政策部分不够准确，需要多来源验证",
         "execution_output": "汇报PPT共20页，含资源盘点/市场定位/产品体系三部分"})
check("经历记录+LLM复盘", r.get("reflection_id") and not r.get("reflect_error"),
      str(r)[:150])
print(f"     做得好: {len(r.get('went_well') or [])} 条 | "
      f"问题: {len(r.get('what_failed') or [])} 条")

# 2. 复盘产出 Lessons Learned
lessons = api("GET", f"/reflection/agents/{MKT}/lessons")
check("经验教训提取", len(lessons) >= 1)
if lessons:
    print(f"     教训示例: {lessons[0]['lesson'][:50]}")

# 3. 复盘产出 Improvement Proposal（不自动生效）
refls = api("GET", f"/reflection/agents/{MKT}/reflections")
target = [x for x in refls if x["id"] == r.get("reflection_id")]
check("复盘记录可查", len(target) == 1)
props = target[0].get("proposals", []) if target else []
check("改进提案已生成且待审批", len(props) >= 1 and props[0]["status"] == "PROPOSED")

# 4. 治理审批：拒绝 → 不生效
pid = props[0]["id"]
rr = api("POST", f"/reflection/proposals/{pid}/decide", {},
         {"decision": "reject"})
check("提案拒绝", rr.get("status") == "REJECTED" and rr.get("applied") is False)

# 5. 新提案批准+应用 → 写入学习目标或知识库
r2 = api("POST", "/reflection/experiences", {"agent_id": MKT},
         {"agent_id": MKT, "title": "竞品分析任务",
          "outcome": "SUCCESS",
          "summary": "完成3个竞品对比分析",
          "execution_output": "输出对比矩阵，覆盖定价/功能/渠道"})
if r2.get("proposal_ids"):
    p2 = r2["proposal_ids"][0]
    ra = api("POST", f"/reflection/proposals/{p2}/decide", {},
             {"decision": "approve", "apply": True})
    check("提案批准+应用", ra.get("status") == "APPROVED" and ra.get("applied") is True)
else:
    check("提案批准+应用", False, "无提案生成")

# 6. 成长时间线包含 REFLECTED 事件
prof = api("GET", f"/learning/agents/{MKT}/profile")
types = [e["type"] for e in prof["growth_events"]]
check("成长事件 REFLECTED 记录", "REFLECTED" in types)

print(f"\n=== P3-03 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
