"""P2-10 Advanced Human Intervention end-to-end test."""
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
        print(f"HTTP {e.code}: {e.read().decode()[:200]}")
        raise


print("=== P2-10 Advanced Human Intervention ===")

# 1. 默认策略 level=2：task_create 需要审批
c = api("POST", "/intervention/check",
        {"company_id": CID, "action_type": "task_create"})
assert c["required"] is True and c["level"] == 2
print(f"[1] 默认 L2: task_create 需审批 OK")

# 2. 创建审批请求 → PENDING → 批准
r = api("POST", "/intervention/requests", {"company_id": CID},
        {"action_type": "task_create", "requested_by_agent_id": CEO,
         "payload": {"title": "新任务"}, "reason": "创建市场调研任务"})
assert r["approved"] is False and r["status"] == "PENDING"
rid = r["request_id"]

d = api("POST", f"/intervention/requests/{rid}/decide", {},
        {"decision": "approve", "note": "同意"})
assert d["status"] == "APPROVED"
print("[2] 审批请求 PENDING→APPROVED OK")

# 3. 升级到 L3（高度自动）：task_create 不再需要审批，escalate 需要
api("PUT", "/intervention/policy", {"company_id": CID}, {"level": 3})
c = api("POST", "/intervention/check", {"company_id": CID, "action_type": "task_create"})
assert c["required"] is False
c = api("POST", "/intervention/check", {"company_id": CID, "action_type": "escalate"})
assert c["required"] is True
print("[3] L3 策略: 常规自动 / escalate 需审批 OK")

# 4. L3 下 task_create 自动放行
r = api("POST", "/intervention/requests", {"company_id": CID},
        {"action_type": "task_create", "requested_by_agent_id": CEO})
assert r["approved"] is True and r.get("auto") is True
print("[4] L3 下 task_create 自动放行 OK")

# 5. Agent 强制请求审批（force）
r = api("POST", "/intervention/requests", {"company_id": CID},
        {"action_type": "task_create", "force": True,
         "reason": "金额较大，Agent 拿不准"})
assert r["approved"] is False
rid2 = r["request_id"]
api("POST", f"/intervention/requests/{rid2}/decide", {}, {"decision": "reject"})
print("[5] force 请求审批 + 驳回 OK")

# 6. L0 全手动：一切都要批
api("PUT", "/intervention/policy", {"company_id": CID}, {"level": 0})
c = api("POST", "/intervention/check", {"company_id": CID, "action_type": "anything"})
assert c["required"] is True
print("[6] L0 全手动: 一切动作需审批 OK")

# 7. L4 全自动
api("PUT", "/intervention/policy", {"company_id": CID}, {"level": 4})
c = api("POST", "/intervention/check", {"company_id": CID, "action_type": "escalate"})
assert c["required"] is False
print("[7] L4 全自动: 无需审批 OK")

# 恢复默认 L2
api("PUT", "/intervention/policy", {"company_id": CID}, {"level": 2})

# 8. 人工覆盖日志
o = api("POST", "/intervention/override", {"company_id": CID},
        {"target_type": "meeting_decision", "target_id": "fake-decision-id",
         "original_decision": {"choice": "切入文旅"},
         "override_decision": {"choice": "切入中小企业办公"},
         "reason": "老板最终拍板"})
ov = api("GET", "/intervention/overrides", {"company_id": CID})
assert len(ov) >= 1 and ov[0]["override"]["choice"] == "切入中小企业办公"
print(f"[8] 覆盖日志 OK（共 {len(ov)} 条）")

print("\n=== P2-10 E2E PASSED ===")
