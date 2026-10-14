"""P2-04 meeting system end-to-end test."""
import json
import time
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
PID = open("/tmp/aieos_pid").read().strip()


def call(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=60).read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode()[:200]}


agents = call(f"/agents?company_id={CID}")
ceo = next(a for a in agents if "CEO" in a["name"])
strat = next(a for a in agents if "战略" in a["name"])
mkt = next(a for a in agents if "市场" in a["name"])

m = call("/meetings", "POST", {
    "company_id": CID, "project_id": PID,
    "title": "AI-EOS 市场切入点评审会",
    "topic": "讨论产品应优先切入哪个目标市场",
    "context_material": "AI-EOS 是多智能体企业操作系统。候选方向：A) 中小企业自动化办公；B) 文旅行业数字化；C) 开发者工具平台。",
    "meeting_type": "STRATEGY_MEETING",
    "participant_ids": [ceo["id"], strat["id"], mkt["id"]],
})
print("created:", m)
mid = m["id"]

print("start:", call(f"/meetings/{mid}/start", "POST"))

# 会议需要 3 轮（开场/讨论/总结），每轮 3 位 Agent 发言
for i in range(40):
    time.sleep(15)
    d = call(f"/meetings/{mid}")
    n_msgs = len(d.get("messages", []))
    print(f"[{i*15}s] status={d['status']} messages={n_msgs}")
    if d["status"] in ("COMPLETED", "CANCELLED"):
        break

d = call(f"/meetings/{mid}")
print("\n=== 发言记录 ===")
for msg in d["messages"]:
    print(f"  [{msg['speaker']}] {msg['content'][:80]}")
print("\n=== 决策 ===")
for dec in d["decisions"]:
    print(f"  ✅ {dec['content'][:100]}")
print("\n=== 行动项 ===")
for it in d["action_items"]:
    print(f"  📌 {it['content'][:100]} → {it['assignee']} ({it['status']})")

# 行动项转任务
if d["action_items"]:
    conv = call(f"/meetings/{mid}/action-items/convert", "POST")
    print("\n转任务:", conv)
