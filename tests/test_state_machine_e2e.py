"""P1-14 state machine + pause/resume/cancel integration test."""
import json
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def call(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body else None, headers=AUTH)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "detail": e.read().decode()[:200]}


# 1. create task
t = call("/tasks", "POST", {"company_id": CID,
                            "title": "状态机测试-暂停恢复取消",
                            "description": "P1-14 verification"})
tid = t["id"]
print("created:", tid, "status:", t["status"])

# 2. illegal transition attempt via PATCH (should be ignored by allowed keys
#    but status change must respect machine)
r = call(f"/tasks/{tid}/pause", "POST")
print("pause TODO (legal):", r)

r = call(f"/tasks/{tid}", "PATCH", {"status": "COMPLETED"})
print("patch status COMPLETED from PAUSED -> should be rejected or no-op")

r = call(f"/tasks/{tid}/resume", "POST")
print("resume PAUSED:", r["detail"] if isinstance(r, dict) and "detail" in r else r)

r = call(f"/tasks/{tid}/cancel?reason=integration-test", "POST")
print("cancel:", r)

detail = call(f"/tasks/{tid}")
print("final status:", detail["status"])
print("event trail:")
for e in detail["events"]:
    print("  ", e["type"], e["payload"])
