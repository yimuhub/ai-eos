"""P2-07 Agent Memory System end-to-end test."""
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
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        raise


print("=== P2-07 Agent Memory ===")

# 1. 写入 5 种类型记忆
memories = [
    ("EPISODIC", "今天完成了市场切入点评审会，决定优先切入文旅市场", 0.8),
    ("SEMANTIC", "公司主营业务是 AI 企业操作系统，核心产品叫 AI-EOS", 0.9),
    ("PROCEDURAL", "开会的流程：先开场发言，再讨论一轮，最后总结并提取 DECISION/ACTION", 0.7),
    ("WORKING", "当前正在处理 P2 阶段的开发任务", 0.5),
    ("REFLECTION", "上次直接发消息没有调度下一轮导致会议卡住，教训：每轮结束必须显式调度下一步", 0.85),
]
ids = []
for mtype, content, imp in memories:
    r = api("POST", "/memory/remember", {"company_id": CID},
            {"agent_id": CEO, "memory_type": mtype, "content": content,
             "importance": imp})
    assert r.get("stored"), f"{mtype} 未存储: {r}"
    ids.append(r["id"])
print(f"[1] 写入 5 种记忆 OK（TTL 已自动设置）")

# 2. 重要度过滤：低重要度被拒
r = api("POST", "/memory/remember", {"company_id": CID},
        {"agent_id": CEO, "memory_type": "SEMANTIC", "content": "随便记一笔",
         "importance": 0.1})
assert not r["stored"]
print("[2] 低重要度记忆被过滤 OK")

# 3. 按查询召回
rec = api("GET", "/memory/recall",
          {"company_id": CID, "agent_id": CEO, "q": "会议流程怎么走"})
assert rec["count"] > 0
top = rec["memories"][0]
print(f"[3] 召回'会议流程': top1 [{top['type']}] {top['content'][:30]}...")

# 4. 无查询召回（按重要度）
rec_all = api("GET", "/memory/recall", {"company_id": CID, "agent_id": CEO})
types_seen = {m["type"] for m in rec_all["memories"]}
print(f"[4] 全量召回 {rec_all['count']} 条，覆盖类型: {types_seen}")

# 5. 按类型过滤
rec_sem = api("GET", "/memory/recall",
              {"company_id": CID, "agent_id": CEO, "memory_type": "SEMANTIC"})
assert all(m["type"] == "SEMANTIC" for m in rec_sem["memories"])
print(f"[5] SEMANTIC 过滤召回 {rec_sem['count']} 条 OK")

# 6. 遗忘
ok = api("DELETE", f"/memory/{CEO}/{ids[3]}")
print("[6] 遗忘 WORKING 记忆 OK")

# 7. 清理过期（无过期数据也应正常返回）
c = api("POST", "/memory/cleanup", {"company_id": CID})
print(f"[7] 过期清理: removed={c['removed']}")

# 确认删除后数量减少
rec_final = api("GET", "/memory/recall", {"company_id": CID, "agent_id": CEO})
assert rec_final["count"] == rec_all["count"] - 1
print(f"[8] 最终记忆数: {rec_final['count']}（已减 1）")

print("\n=== P2-07 E2E PASSED ===")
