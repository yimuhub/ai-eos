"""P3-09 External Agent Integration end-to-end test."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()

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
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=== P3-09 External Agent Integration ===")

# 1. 注册本地 LLM Agent（llama.cpp，OpenAI 兼容）
r = api("POST", "/external-agents", {"company_id": CID},
        {"name": "本地Qwen (llama.cpp)", "provider_type": "LOCAL",
         "endpoint": "http://192.168.3.39:8080/v1",
         "auth_env_var": "LOCAL_LLM_KEY",   # 只存 env 名，不存明文
         "capabilities": ["chat", "reasoning"]})
check("注册外部Agent", r.get("id"))
cid1 = r.get("id")

# 2. 列表确认 auth 不落明文（只有 env var 名）
lst = api("GET", "/external-agents", {"company_id": CID})
row = [x for x in lst if x["id"] == cid1][0]
check("密钥仅存环境变量名", row["auth_env_var"] == "LOCAL_LLM_KEY"
      and "sk-" not in json.dumps(row))

# 3. 未启用时 execute 被拒
r = api("POST", f"/external-agents/{cid1}/execute", {},
        {"task_input": "你好"})
check("未启用执行被拒", r.get("__http_error__") == 409)

# 4. 启用后测试连接（本机 llama-server 可能没开，允许 ok 或失败但接口正常）
api("POST", f"/external-agents/{cid1}/enable")
t = api("POST", f"/external-agents/{cid1}/test")
check("连接测试接口正常", "ok" in t or "error" in t)

# 5. 执行（llama-server 若在线则真实返回；离线则 502——两种都算接口正确）
r = api("POST", f"/external-agents/{cid1}/execute", {},
        {"task_input": "用10个字介绍你自己"})
check("统一Adapter执行(在线成功/离线502)",
      "output" in r or r.get("__http_error__") == 502,
      str(r)[:80])

# 6. 禁用 → 删除
api("POST", f"/external-agents/{cid1}/disable")
d = api("DELETE", f"/external-agents/{cid1}")
check("禁用+删除 OK", d.get("deleted") is True)

print(f"\n=== P3-09 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0
