"""P2-09 Artifact Collaboration end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"
STRAT = "d7e8607b-95a7-400f-b8ac-e83830d3e76f"


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


print("=== P2-09 Artifact Collaboration ===")

# 1. 创建成果（v1 草稿）
art = api("POST", "/artifacts", {"company_id": CID},
          {"name": "文旅项目策划方案", "artifact_type": "document",
           "content": "v1 初稿：项目概述...", "submitted_by": CEO})
aid = art["id"]
assert art["status"] == "DRAFT" and art["current_version"] == 1
print(f"[1] 创建成果 v1 DRAFT: {art['name']}")

# 2. 非法流转：DRAFT 直接 approve 应被拒
try:
    api("POST", f"/artifacts/{aid}/review", {"company_id": CID},
        {"decision": "approve"})
    print("[2] FAIL: 不该允许 DRAFT→APPROVED")
    raise SystemExit(1)
except urllib.error.HTTPError as e:
    assert e.code == 409
    print("[2] 状态机守卫 OK：DRAFT 不能直接 approve (409)")

# 3. 提交审核
r = api("POST", f"/artifacts/{aid}/submit", {"company_id": CID},
        {"content": "v2 完善版：加入市场分析...", "change_note": "补充市场分析",
         "submitted_by_agent_id": CEO})
assert r["status"] == "IN_REVIEW" and r["current_version"] == 2
print(f"[3] 提交审核 OK: v{r['current_version']} IN_REVIEW")

# 4. 驳回
r = api("POST", f"/artifacts/{aid}/review", {"company_id": CID},
        {"decision": "reject", "comment": "竞品分析不够深入",
         "reviewer_agent_id": STRAT})
assert r["status"] == "REJECTED"
print("[4] 审核驳回 OK（附意见）")

# 5. 重新提交 + 批准
api("POST", f"/artifacts/{aid}/submit", {"company_id": CID},
    {"content": "v3 终版：深化竞品分析...", "change_note": "响应审核意见",
     "submitted_by_agent_id": CEO})
r = api("POST", f"/artifacts/{aid}/review", {"company_id": CID},
        {"decision": "approve", "comment": "通过", "reviewer_agent_id": STRAT})
assert r["status"] == "APPROVED"
print("[5] 修改后重新提交并批准 OK")

# 6. 版本历史
detail = api("GET", f"/artifacts/{aid}", {"company_id": CID})
vers = detail["versions"]
assert len(vers) == 3 and detail["status"] == "APPROVED"
print(f"[6] 版本历史 {len(vers)} 个: " +
      ", ".join(f"v{v['version']}({v['status']})" for v in vers))

# 7. 读取指定版本内容
c = api("GET", f"/artifacts/{aid}/versions/1", {"company_id": CID})
assert "初稿" in c["content"]
print(f"[7] 历史版本内容读取 OK: v1 = {c['content'][:20]}...")

# 清理
import urllib.request as _u
req = _u.Request(f"{BASE}/artifacts/{aid}?company_id={CID}", method="DELETE")
req.add_header("Authorization", f"Bearer {TOKEN}")
try:
    _u.urlopen(req)
except Exception:
    pass  # 无 delete 接口也行，测试数据保留无妨

print("\n=== P2-09 E2E PASSED ===")
