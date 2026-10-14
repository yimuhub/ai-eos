"""P3-02 Agent Training System end-to-end test."""
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


print("=== P3-02 Agent Training System ===")

# 记录训练前档案状态
before = api("GET", f"/learning/agents/{MKT}/profile")
caps_before = list(before["profile"]["capabilities"])
prefs_before = dict(before["profile"]["preferences"])

# 1. 创建训练会话（对话训练）→ LLM 自动提取
s = api("POST", "/training/sessions", {"agent_id": MKT},
        {"training_type": "CONVERSATION",
         "title": "文旅项目汇报规范",
         "raw_input": "以后做政府文旅项目汇报时：1)必须引用具体政策文号，如文旅部2024年相关文件；"
                      "2)分析框架优先使用SWOT；3)汇报结构采用'资源盘点-市场定位-产品体系-投资测算'四段式。"})
check("会话创建+LLM提取", s.get("status") == "EXTRACTED" and s.get("points", 0) >= 1,
      str(s)[:150])
sid = s.get("id")

# 2. 查看学到了什么（Preview）
d = api("GET", f"/training/sessions/{sid}")
check("Learning Points 已提取", len(d.get("learning_points", [])) >= 1)
check("Proposals 待审批", all(p["status"] == "PROPOSED"
                              for p in d.get("proposals", [])))
print(f"     提取摘要: {d.get('summary', '')[:60]}")

# 3. 批准 → 应用到档案/知识库
r = api("POST", f"/training/sessions/{sid}/approve")
check("批准并应用", r.get("status") == "APPLIED" and r.get("applied_count", 0) >= 1)

# 4. 验证生效：知识库出现训练文档 / 偏好更新
kb_docs = api("GET", "/knowledge/documents", {"company_id": CID})
train_doc = [x for x in kb_docs if x["title"].startswith("[训练]")]
check("知识库写入训练文档", len(train_doc) >= 1)

after = api("GET", f"/learning/agents/{MKT}/profile")
growth_types = [e["type"] for e in after["growth_events"]]
check("成长事件 TRAINED 记录", "TRAINED" in growth_types)

# 5. 拒绝流程：创建第二个训练，拒绝它，验证不生效
s2 = api("POST", "/training/sessions", {"agent_id": MKT},
         {"training_type": "FEEDBACK", "title": "不应生效的训练",
          "raw_input": "请把偏好改为 daily_report=true"})
check("第二会话提取", s2.get("status") == "EXTRACTED")
r = api("POST", f"/training/sessions/{s2['id']}/reject")
check("拒绝 OK", r.get("status") == "REJECTED")

# 6. 回滚第一次应用的变更
d1 = api("GET", f"/training/sessions/{sid}")
apps = d1.get("applications", [])
if apps:
    rb = api("POST", f"/training/applications/{apps[0]['id']}/rollback")
    check("回滚训练应用", rb.get("rolled_back") is True)
    # 知识库中训练文档应被删除
    kb_after = api("GET", "/knowledge/documents", {"company_id": CID})
    still = [x for x in kb_after if x["title"].startswith("[训练]")]
    check("回滚后知识删除", len(still) == 0)

# 清理训练产生的知识文档
for x in train_doc:
    try:
        api("DELETE", f"/knowledge/documents/{x['id']}")
    except Exception:
        pass

print(f"\n=== P3-02 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
