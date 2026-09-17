"""P2-11 Agent Performance end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"


def api(method, path, params=None, data=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


print("=== P2-11 Agent Performance ===")

# 1. 单 Agent 实时绩效（真实数据：CEO 参加过会议、发过协作消息）
m = api("GET", f"/performance/agents/{CEO}", {"company_id": CID})
print(f"[1] CEO 实时绩效: tasks={m['tasks_total']} 完成={m['tasks_completed']} "
      f"失败={m['tasks_failed']} 发言={m['meeting_spoke_count']} "
      f"消息={m['messages_sent']} score={m['score']}")
assert m["tasks_total"] >= 0 and m["messages_sent"] > 0

# 2. 全公司快照（排行榜）
rank = api("POST", f"/performance/company/{CID}/snapshot")
assert len(rank["ranked"]) == 4
print("[2] 公司排行榜:")
for i, r in enumerate(rank["ranked"], 1):
    print(f"    {i}. {r['agent_name']}: score={r['score']} "
          f"(任务{r['tasks_total']}/完成{r['tasks_completed']}, "
          f"发言{r['meeting_spoke_count']}, 消息{r['messages_sent']})")

# 3. 历史快照
hist = api("GET", f"/performance/company/{CID}/history", {"limit": 10})
assert len(hist) >= 4
print(f"[3] 历史快照 {len(hist)} 条 OK")

# 4. 排序验证：分数降序
scores = [r["score"] for r in rank["ranked"]]
assert scores == sorted(scores, reverse=True)
print("[4] 排行榜排序正确（降序）")

print("\n=== P2-11 E2E PASSED ===")
