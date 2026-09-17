"""P2-15 Automation Scheduler end-to-end test."""
import json
import time
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
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {"__http_error__": e.code, "detail": body}


print("=== P2-15 Automation Scheduler ===")

# 1. 创建 interval 调度（10s，立即到期）→ 绩效快照动作
s = api("POST", "/schedules", {"company_id": CID},
        {"name": "每日绩效快照(测试)", "trigger_type": "interval",
         "seconds": 3600, "action_type": "snapshot_performance"})
assert s["next_run_at"]
sid_interval = s["id"]
print(f"[1] 创建 interval 调度: {sid_interval[:8]} next_run={s['next_run_at'][:19]}")

# 2. 创建 daily 调度
s2 = api("POST", "/schedules", {"company_id": CID},
         {"name": "每日晨报消息", "trigger_type": "daily", "time": "23:59",
          "action_type": "send_message",
          "action_params": {"sender_agent_id": CEO,
                            "title": "晨间简报", "content": "各位早上好，开始今天的工作"}})
sid_daily = s2["id"]
print(f"[2] 创建 daily 调度: next_run={s2['next_run_at'][:19]}")

# 3. 手动 tick 触发 interval 调度（把 next_run 设为过去以模拟到期）
#    直接调 tick：interval<=30s 的调度创建时立即到期，但这里 3600s 未到期。
#    为测试，用 pause/resume 把 next_run 重置为 now。
api("POST", f"/schedules/{sid_interval}/pause")
api("POST", f"/schedules/{sid_interval}/resume")   # resume 会设 next_run=now
r = api("POST", "/schedules/tick", {"company_id": CID})
assert r["executed"] >= 1, r
print(f"[3] 手动 tick 执行了 {r['executed']} 个到期调度")

# 4. 查看运行记录
time.sleep(1)
runs = api("GET", f"/schedules/{sid_interval}/runs")
assert runs and runs[0]["status"] == "SUCCESS", runs[:1]
print(f"[4] 运行记录 SUCCESS: result={runs[0]['result']}")

# 5. run_count 增长
scheds = api("GET", "/schedules", {"company_id": CID})
target = [x for x in scheds if x["id"] == sid_interval][0]
assert target["run_count"] >= 1 and target["last_run_at"]
print(f"[5] run_count={target['run_count']} last_run 已更新 OK")

# 6. 暂停后 tick 不再执行
api("POST", f"/schedules/{sid_interval}/pause")
r = api("POST", "/schedules/tick", {"company_id": CID})
assert r["executed"] == 0, r
print("[6] PAUSED 调度不再执行 OK")

# 清理
for sid in (sid_interval, sid_daily):
    api("DELETE", f"/schedules/{sid}")
print("[7] 测试调度已归档")

print("\n=== P2-15 E2E PASSED ===")
