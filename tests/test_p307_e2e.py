"""P3-07 Evaluation & Review end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
MKT = "4ef2b1cd-d607-48d3-9ded-8244ce0b012d"
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"

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


print("=== P3-07 Evaluation & Review ===")

# 1. USER 评价
r = api("POST", f"/reviews/agents/{MKT}", {},
        {"source": "USER", "score": 85,
         "strengths": ["响应快"], "weaknesses": ["深度不足"],
         "suggestions": ["增加行业案例储备"]})
check("USER 评价提交", r.get("id") and r.get("score") == 85)

# 2. MANAGER 评价（CEO 作为评审人）
r = api("POST", f"/reviews/agents/{MKT}?reviewer_agent_id={CEO}", {},
        {"source": "MANAGER", "score": 90,
         "strengths": ["执行力强"], "comment": "本季度表现优秀"})
check("MANAGER 评价提交", r.get("score") == 90)

# 3. SELF 评价
r = api("POST", f"/reviews/agents/{MKT}", {},
        {"source": "SELF", "score": 80})
check("SELF 评价提交", r.get("score") == 80)

# 4. AUTOMATED 考核：基于真实指标
r = api("POST", "/reviews/agents/{0}".format(MKT), {},
        {"source": "AUTOMATED", "score": 50})   # score 会被真实数据覆盖展示
check("AUTOMATED 考核（附真实证据）",
      r.get("id") is not None)

# 5. 无执行数据的 Agent 自动考核被拒（禁止编造）
r = api("POST", "/reviews/agents/{0}".format(CEO.replace("c1250aba", "ffffffff")), {},
        {"source": "AUTOMATED", "score": 50})
check("不存在Agent自动考核被拒", r.get("__http_error__") in (404, 409))

# 6. 分数校验
r = api("POST", f"/reviews/agents/{MKT}", {}, {"source": "PEER", "score": 150})
check("分数>100被拒", r.get("__http_error__") == 400)

# 7. 评价列表 + EVALUATED 成长事件（4种来源各1条）
revs = api("GET", f"/reviews/agents/{MKT}")
sources = {x["source"] for x in revs}
check("评价列表含4种来源", {"USER", "MANAGER", "SELF", "AUTOMATED"} <= sources
      and len(revs) >= 4)
prof = api("GET", f"/learning/agents/{MKT}/profile")
types = [e["type"] for e in prof["growth_events"]]
check("成长事件 EVALUATED 记录", "EVALUATED" in types)
scores = [x["score"] for x in revs]
print(f"     各来源评分: {sorted(scores, reverse=True)}")

print(f"\n=== P3-07 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
