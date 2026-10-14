"""P2-02 workflow engine end-to-end test.

Builds a real workflow: START -> AGENT(市场研究员) -> CONDITION ->
  yes -> AGENT(战略分析师) -> END
  no  -> END
Runs it and verifies persisted steps.
"""
import json
import time
import urllib.request

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
        return {"_err": e.code, "_body": e.read().decode()[:300]}


# 取两个 agent
agents = call(f"/agents?company_id={CID}")
mkt = next(a for a in agents if "市场" in a["name"])
strat = next(a for a in agents if "战略" in a["name"])

wf = call("/workflows?company_id=" + CID, "POST", {
    "name": "市场研判-条件分支测试流",
    "description": "START → 市场速览 → 条件判断 → 战略分析 → END",
    "project_id": PID,
    "nodes": [
        {"node_key": "n1", "node_type": "START", "name": "开始"},
        {"node_key": "n2", "node_type": "AGENT", "name": "市场速览",
         "config": {"agent_id": mkt["id"],
                    "task_title_template": "[工作流]市场速览",
                    "instruction": "50字以内概述AI-EOS市场机会"}},
        {"node_key": "n3", "node_type": "CONDITION", "name": "是否有机会",
         "config": {"keyword": "市场"}},
        {"node_key": "n4", "node_type": "AGENT", "name": "战略分析",
         "config": {"agent_id": strat["id"],
                    "task_title_template": "[工作流]战略分析",
                    "instruction": "基于市场速览给出30字战略建议"}},
        {"node_key": "n5", "node_type": "END", "name": "结束"},
    ],
    "edges": [
        {"from_node": "n1", "to_node": "n2"},
        {"from_node": "n2", "to_node": "n3"},
        {"from_node": "n3", "to_node": "n4", "label": "yes"},
        {"from_node": "n3", "to_node": "n5", "label": "no"},
        {"from_node": "n4", "to_node": "n5"},
    ],
})
print("workflow saved:", wf.get("id", wf))

wid = wf["id"]
run = call(f"/workflows/{wid}/run?project_id={PID}", "POST")
print("run:", run)
eid = run["execution_id"]

for i in range(24):
    time.sleep(10)
    ex = call(f"/executions/{eid}")
    print(f"[{i*10}s] status={ex['status']} steps={len(ex['steps'])}")
    if ex["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break

ex = call(f"/executions/{eid}")
print("\n=== 执行轨迹 ===")
for s in ex["steps"]:
    out = (s.get("output_data") or {})
    print(f"  {s['node_name']:8s} {s['status']:9s} attempt={s['attempt']} "
          f"out={json.dumps(out, ensure_ascii=False)[:100]}")
print("final:", ex["status"])
