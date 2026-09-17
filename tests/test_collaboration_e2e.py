"""P2-05: Agent Collaboration Protocol 2.0 end-to-end test."""
import json
import urllib.request
import time

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
PID = open("/tmp/aieos_pid").read().strip()
AUTH = f"Authorization: Bearer {TOKEN}"


def api(method, path, data=None):
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode()[:200]}")
        raise


print("=== P2-05: Agent Collaboration Protocol 2.0 ===")

# 获取所有 Agent
agents = api("GET", f"/agents?company_id={CID}")
print(f"Agents: {len(agents)}")
for a in agents:
    print(f"  - {a['name']} ({a['id'][:8]})")

ceo = next(a for a in agents if "CEO" in a["name"])
strat = next(a for a in agents if "战略" in a["name"])
mkt = next(a for a in agents if "市场" in a["name"])

# 1. 创建协作关系
print("\n=== Creating Collaboration Graph ===")
collab1 = api("POST", f"/collaboration/collaborations?company_id={CID}", {
    "source_agent_id": ceo["id"],
    "target_agent_id": strat["id"],
    "collaboration_type": "manager",
    "description": "CEO manages 战略分析师"
})
print(f"Created: {collab1['source_agent_id'][:8]} -> {collab1['target_agent_id'][:8]} ({collab1['collaboration_type']})")

collab2 = api("POST", f"/collaboration/collaborations?company_id={CID}", {
    "source_agent_id": ceo["id"],
    "target_agent_id": mkt["id"],
    "collaboration_type": "manager",
    "description": "CEO manages 市场研究员"
})
print(f"Created: {collab2['source_agent_id'][:8]} -> {collab2['target_agent_id'][:8]} ({collab2['collaboration_type']})")

collab3 = api("POST", f"/collaboration/collaborations?company_id={CID}", {
    "source_agent_id": strat["id"],
    "target_agent_id": mkt["id"],
    "collaboration_type": "collaborator",
    "description": "战略分析师 collaborates with 市场研究员"
})
print(f"Created: {collab3['source_agent_id'][:8]} -> {collab3['target_agent_id'][:8]} ({collab3['collaboration_type']})")

# 2. 获取协作图谱
print("\n=== Getting Collaboration Graph ===")
graph = api("GET", f"/collaboration/agents/{ceo['id']}/graph?company_id={CID}")
print(f"CEO connections: {len(graph['connections'])}")
for conn in graph['connections']:
    print(f"  {conn['type']}: {conn['source_agent_name'] if conn['type'] == 'incoming' else conn['target_agent_name']} ({conn['collaboration_type']})")

# 3. 发送 12 种类型的消息
print("\n=== Sending Collaboration Messages (12 types) ===")
message_types = [
    "TASK_ASSIGNED",
    "TASK_COMPLETED",
    "DISCUSSION",
    "VOTE",
    "DECISION",
    "CONFLICT",
    "QUESTION",
    "ANSWER",
    "PROGRESS",
    "BLOCKED",
    "ESCALATE",
    "DONE",
]

messages = []
for msg_type in message_types:
    msg = api("POST", f"/collaboration/messages?company_id={CID}", {
        "sender_agent_id": ceo["id"],
        "message_type": msg_type,
        "title": f"Test {msg_type}",
        "content": f"This is a test {msg_type} message from CEO",
        "context_ref": PID,
        "context_type": "project"
    })
    messages.append(msg)
    print(f"  {msg_type}: {msg['id'][:8]} -> receivers: {[r[:8] for r in (msg['receiver_agent_ids'] or [])]}")

# 4. 获取消息列表
print("\n=== Listing Messages ===")
msgs = api("GET", f"/collaboration/messages?company_id={CID}&limit=5")
print(f"Recent 5 messages: {len(msgs)}")
for m in msgs:
    print(f"  [{m['message_type']}] {m['sender_name']} -> {m['title'][:30]}")

# 5. 获取消息详情
print("\n=== Message Detail ===")
msg_detail = api("GET", f"/collaboration/messages/{messages[0]['id']}?company_id={CID}")
print(f"Message: {msg_detail['message_type']} from {msg_detail['sender_name']}")
print(f"Receivers: {msg_detail['receiver_agent_ids']}")

# 6. 获取统计
print("\n=== Collaboration Stats ===")
stats = api("GET", f"/collaboration/stats/{CID}")
print(f"Total collaborations: {stats['collaboration_count']}")
print(f"Total messages: {stats['message_count']}")
print(f"Message types: {stats['message_type_counts']}")

print("\n=== P2-05 E2E Test PASSED ===")
