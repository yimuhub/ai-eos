"""P2-14 Computer Agent end-to-end test."""
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
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {"__http_error__": e.code, "detail": body}


print("=== P2-14 Computer Agent ===")

# 1. 未授权时执行被拒
r = api("POST", "/computer-agent/execute",
        {"company_id": CID, "agent_id": CEO},
        {"action_type": "screenshot"})
assert r.get("__http_error__") == 403
print("[1] 未授权 screenshot 被拒 (403) OK")

# 2. 授权后 Mock 执行成功
api("POST", "/computer-agent/grant", {"company_id": CID, "capability": "screenshot"})
r = api("POST", "/computer-agent/execute",
        {"company_id": CID, "agent_id": CEO}, {"action_type": "screenshot"})
assert r.get("mode") == "MOCK" and r["result"]["mock"] is True
print(f"[2] 授权后截图(Mock) OK: {r['result']}")

# 3. 其他能力仍未授权
r = api("POST", "/computer-agent/execute",
        {"company_id": CID, "agent_id": CEO},
        {"action_type": "type_text", "params": {"text": "hello"}})
assert r.get("__http_error__") == 403
print("[3] 未授权 type_text 仍被拒 OK（权限按能力隔离）")

# 4. 授权 click 并执行
api("POST", "/computer-agent/grant", {"company_id": CID, "capability": "click"})
r = api("POST", "/computer-agent/execute", {"company_id": CID, "agent_id": CEO},
        {"action_type": "click", "params": {"x": 100, "y": 200}})
assert r["result"]["clicked_at"] == [100, 200]
print("[4] click(Mock) OK")

# 5. 权限列表
perms = api("GET", "/computer-agent/permissions", {"company_id": CID})
assert perms["screenshot"] is True and perms["type_text"] is False
print(f"[5] 权限列表: {perms}")

# 6. 撤销后再执行被拒
api("POST", "/computer-agent/revoke", {"company_id": CID, "capability": "click"})
r = api("POST", "/computer-agent/execute", {"company_id": CID, "agent_id": CEO},
        {"action_type": "click"})
assert r.get("__http_error__") == 403
print("[6] 撤销 click 后被拒 OK")

# 7. 审计日志完整（含拒绝记录）
logs = api("GET", "/computer-agent/logs", {"company_id": CID})
denied = [l for l in logs if not l["allowed"]]
allowed = [l for l in logs if l["allowed"]]
assert len(denied) >= 3 and len(allowed) >= 2
print(f"[7] 审计日志 {len(logs)} 条: 允许 {len(allowed)}, 拒绝 {len(denied)}")

# 清理授权状态
for cap in ("screenshot", "click"):
    api("POST", "/computer-agent/revoke", {"company_id": CID, "capability": cap})

print("\n=== P2-14 E2E PASSED ===")
