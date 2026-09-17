"""P3-10 + P3-11 Sandbox & Computer Runtime end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
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


print("=== P3-10/11 Sandbox & Computer Runtime ===")

# 1. 创建 Mock 工作区
w = api("POST", "/sandbox/workspaces",
        {"company_id": CID, "name": "测试沙箱", "provider": "MOCK"})
check("创建沙箱工作区", w.get("id"))
wid = w["id"]

# 2. 启动会话
s = api("POST", f"/sandbox/workspaces/{wid}/sessions",
        {"agent_id": CEO}, {"goal": "打开备忘录写一份周报并保存"})
sid = s["id"]
check("启动会话", s.get("status") == "RUNNING")

# 3. Action Loop：截图 → 打开文件 → 输入 → 保存
a1 = api("POST", f"/sandbox/sessions/{sid}/act",
         {"approved": True}, {"action_type": "screenshot"})
check("Observe(截图)", a1.get("seq") == 1 and a1["screenshots"]["before"])
a2 = api("POST", f"/sandbox/sessions/{sid}/act",
         {"approved": True}, {"action_type": "open_file", "params": {"path": "/apps/notes"}})
check("Act(打开应用)", a2.get("seq") == 2)
a3 = api("POST", f"/sandbox/sessions/{sid}/act",
         {"approved": True}, {"action_type": "type", "params": {"text": "本周完成P3开发"}})
check("Act(键入)", a3.get("seq") == 3)

# 4. 暂停 → act 被拒 → 恢复（Checkpoint）
api("POST", f"/sandbox/sessions/{sid}/pause")
r = api("POST", f"/sandbox/sessions/{sid}/act", {"approved": True}, {"action_type": "click"})
check("暂停后操作被拒", r.get("__http_error__") == 409)
rr = api("POST", f"/sandbox/sessions/{sid}/resume")
check("从Checkpoint恢复", rr.get("resumed_from_checkpoint") is True)

# 5. 高风险动作：delete_file 未批准 → requires_approval
r = api("POST", f"/sandbox/sessions/{sid}/act", {"approved": False},
        {"action_type": "delete_file", "params": {"path": "/important.txt"}})
check("高风险动作需审批", r.get("requires_approval") is True)
pending_id = r.get("action_id")

# 6. 批准后执行
api("POST", f"/sandbox/actions/{pending_id}/approve")
r = api("POST", f"/sandbox/sessions/{sid}/act",
        {"approved": True}, {"action_type": "delete_file",
                             "params": {"path": "/important.txt"}})
check("批准后高风险动作执行", r.get("seq") and not r.get("requires_approval"))

# 7. 保存文件到沙箱 → 文件系统可见
api("POST", f"/sandbox/sessions/{sid}/act",
    {"approved": True}, {"action_type": "save_file",
     "params": {"path": "/reports/weekly.md", "content": "# 周报\n完成P3开发"}})
files = api("GET", f"/sandbox/workspaces/{wid}/files")
match = [f for f in files if f["path"] == "/reports/weekly.md"]
check("文件保存到沙箱", len(match) == 1 and "周报" in match[0]["content"])

# 8. 操作历史含 before/after 截图
actions = api("GET", f"/sandbox/sessions/{sid}/actions")
executed = [a for a in actions if not (a.get("result") or {}).get("pending_approval")]
check(f"已执行操作({len(executed)}步)均含双截图",
      len(executed) >= 4 and all(a["before"] and a["after"] for a in executed))

# 9. 取消会话
rc = api("POST", f"/sandbox/sessions/{sid}/cancel")
check("取消会话", rc.get("status") == "CANCELLED")

print(f"\n=== P3-10/11 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
