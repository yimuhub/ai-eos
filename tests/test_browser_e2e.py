"""P2-13 Browser Agent end-to-end test."""
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


print("=== P2-13 Browser Agent ===")

# 1. 正常抓取（https 公网站点）
r = api("POST", "/browser-agent/fetch",
        {"company_id": CID, "agent_id": CEO},
        {"url": "https://example.com", "max_chars": 2000})
assert "text" in r and "Example Domain" in r["text"], r
print(f"[1] https 抓取 OK: {r['text'][:50]}...")

# 2. SSRF 防护：http 内网地址被拒
r = api("POST", "/browser-agent/fetch", {"company_id": CID, "agent_id": CEO},
        {"url": "http://192.168.3.39:8080/v1"})
assert r.get("__http_error__") == 403
print("[2] http+私网 IP 被拒 (403) OK")

# 3. localhost 被拒
r = api("POST", "/browser-agent/fetch", {"company_id": CID, "agent_id": CEO},
        {"url": "https://localhost:8000/health"})
assert r.get("__http_error__") == 403
print("[3] localhost 被拒 OK")

# 4. 非 https 被拒
r = api("POST", "/browser-agent/fetch", {"company_id": CID, "agent_id": CEO},
        {"url": "http://example.com"})
assert r.get("__http_error__") == 403
print("[4] 纯 http 公网也被拒 OK")

# 5. 审计日志：所有尝试（含被拒的）都有记录
logs = api("GET", "/browser-agent/logs", {"company_id": CID})
assert len(logs) >= 4
denied = [l for l in logs if not l["allowed"]]
allowed = [l for l in logs if l["allowed"]]
assert len(denied) >= 3 and len(allowed) >= 1
print(f"[5] 审计日志 {len(logs)} 条: 允许 {len(allowed)}, 拒绝 {len(denied)}")
for l in denied[:3]:
    print(f"    拒绝: {l['url'][:40]} → {l['deny_reason'][:30]}")

print("\n=== P2-13 E2E PASSED ===")
