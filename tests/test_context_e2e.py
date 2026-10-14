"""P2-08 Project Context end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()


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


print("=== P2-08 Project Context ===")

# 创建一个测试项目
proj = api("POST", "/projects", {"company_id": CID},
           {"name": "P2-08 上下文测试项目", "description": "验证 Context Builder"})
pid = proj["id"]
print(f"[1] 项目创建: {pid[:8]}")

# 加里程碑 + 任务（如有接口）
try:
    api("POST", f"/projects/{pid}/milestones", {"company_id": CID},
        {"name": "M1 需求冻结"})
    print("[2] 里程碑创建 OK")
except Exception as e:
    print(f"[2] 里程碑跳过: {e}")

# 获取结构化上下文
ctx = api("GET", f"/projects/{pid}/context", {"company_id": CID})
assert ctx["project"]["name"] == "P2-08 上下文测试项目"
print(f"[3] 结构化上下文 OK: milestones={len(ctx.get('milestones', []))}, "
      f"tasks={len(ctx.get('tasks', []))}")

# 渲染 prompt
prompt_r = api("GET", f"/projects/{pid}/context/prompt", {"company_id": CID})
p = prompt_r["prompt"]
assert "# 项目上下文" in p and "里程碑" in p
print(f"[4] Prompt 渲染 OK ({len(p)} 字符):")
print("----")
print("\n".join(p.splitlines()[:6]))
print("----")

# 快照：保存两次，验证版本自增
s1 = api("POST", f"/projects/{pid}/context/snapshots",
         {"company_id": CID}, {"summary": "初始快照"})
s2 = api("POST", f"/projects/{pid}/context/snapshots",
         {"company_id": CID}, {"summary": "第二次快照"})
assert s1["version"] == 1 and s2["version"] == 2, f"版本异常: {s1},{s2}"
print(f"[5] 快照 v1/v2 OK")

# 列表 + 详情
snaps = api("GET", f"/projects/{pid}/context/snapshots", {"company_id": CID})
assert len(snaps) == 2
detail = api("GET", f"/projects/{pid}/context/snapshots/{s1['id']}",
             {"company_id": CID})
assert detail["context_data"]["project"]["name"].startswith("P2-08")
print(f"[6] 快照列表 {len(snaps)} 条 + 详情读取 OK")

# 清理测试项目
api("DELETE", f"/projects/{pid}", {"company_id": CID})
print("[7] 测试项目清理 OK")

print("\n=== P2-08 E2E PASSED ===")
