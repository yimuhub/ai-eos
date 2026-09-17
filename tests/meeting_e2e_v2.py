"""P2-04 meeting e2e - direct execution (no background pipe)."""
import json, time, urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
PID = open("/tmp/aieos_pid").read().strip()
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def call(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers=AUTH)
    try:
        raw = urllib.request.urlopen(req, timeout=60).read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode()[:200]}

print("=== Agent IDs ===")
agents = call(f"/agents?company_id={CID}")
for a in agents:
    print(f"  {a['id']}  {a['name']}")
ceo = next(a for a in agents if "CEO" in a["name"])
strat = next(a for a in agents if "战略" in a["name"])
mkt = next(a for a in agents if "市场" in a["name"])

print("\n=== 创建会议 ===")
m = call("/meetings", "POST", {
    "company_id": CID, "project_id": PID,
    "title": "AI-EOS 市场切入点评审会",
    "topic": "产品应优先切入哪个目标市场",
    "context_material": "AI-EOS 是多智能体企业操作系统。候选方向：A) 中小企业自动化办公；B) 文旅行业数字化；C) 开发者工具平台。",
    "meeting_type": "STRATEGY_MEETING",
    "participant_ids": [ceo["id"], strat["id"], mkt["id"]],
})
print(m)
mid = m["id"]

print("\n=== 开始 ===")
print(call(f"/meetings/{mid}/start", "POST"))

print("\n=== 等待会议执行（最多 12 轮 × 10s）===")
for i in range(12):
    time.sleep(10)
    d = call(f"/meetings/{mid}")
    n = len(d.get("messages", []))
    print(f"[{i*10}s] status={d['status']} messages={n}")
    if d["status"] == "COMPLETED":
        break

d = call(f"/meetings/{mid}")
print("\n=== 发言记录 ===")
for msg in d["messages"]:
    print(f"  [{msg['speaker']}] {msg['content'][:90]}")
print(f"\n=== 决策 ({len(d['decisions'])}) ===")
for dec in d["decisions"]:
    print(f"  ✅ {dec['content'][:100]}")
print(f"\n=== 行动项 ({len(d['action_items'])}) ===")
for it in d["action_items"]:
    print(f"  📌 {it['content'][:100]}  [{it['status']}]")

print("\n=== 转任务 ===")
if d["action_items"]:
    conv = call(f"/meetings/{mid}/action-items/convert", "POST")
    print(conv)
else:
    print("(无行动项可转)")

print("\n=== 纪要片段 ===")
if d.get("minutes"):
    lines = d["minutes"].splitlines()
    for ln in lines[:15]:
        print(f"  {ln}")
    print(f"  ... (共 {len(lines)} 行)")
