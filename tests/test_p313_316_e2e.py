"""P3-13~16 Governance/Marketplace/Templates/Autonomous end-to-end test."""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
MKT = "4ef2b1cd-d607-48d3-9ded-8244ce0b012d"

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


print("=== P3-13 Self-Improvement Governance ===")

# 1. 提交改进提案（回答 What/Why/Evidence/Risk/Impact）
imp = api("POST", "/governance/improvements", {"company_id": CID},
          {"agent_id": MKT,
           "what_improved": "系统提示词增加政策核查规则",
           "why": "P3-03 复盘发现政策引用不准确",
           "evidence": {"reflection": "汇报前事实核查", "tasks_reviewed": 3},
           "risk": "低：仅修改 system_prompt",
           "expected_impact": "政策类输出准确率提升"})
check("改进提案提交", imp.get("status") == "PROPOSED")
iid = imp["id"]

# 2. 沙盒测试 + 对比
st = api("POST", f"/governance/improvements/{iid}/sandbox-test?test_score=92&baseline_score=78")
check("沙盒克隆+对比测试", st.get("status") == "TESTED"
      and st["comparison"]["delta"] == 14)

# 3. 未测试直接部署被拒
imp2 = api("POST", "/governance/improvements", {"company_id": CID},
           {"agent_id": MKT, "what_improved": "未测试的改进"})
rd = api("POST", f"/governance/improvements/{imp2['id']}/deploy",
         {}, {"approve": True})
check("未测试部署被拒", rd.get("__http_error__") == 409)

# 4. 批准部署 → 版本化 + 可回滚
dep = api("POST", f"/governance/improvements/{iid}/deploy",
          {}, {"approve": True, "note": "测试提升明显，批准"})
check("批准部署(版本化)", dep.get("status") == "DEPLOYED"
      and dep.get("rollback_version"))
new_ver = dep.get("deployed_version")
old_ver = dep.get("rollback_version")

# 5. 回滚到旧版本
rb = api("POST", f"/governance/agents/{MKT}/rollback/{old_ver}")
check("回滚到基线版本", rb.get("rolled_back_to") == old_ver)

# 6. 部署后 Agent 回到可用状态（回滚不改 status）
agent_detail = [a for a in api("GET", "/agents", {"company_id": CID})
                if a["id"] == MKT][0]
check("回滚后 Agent 正常", agent_detail["status"] in ("IDLE", "ACTIVE"))

print("\n=== P3-14 Marketplace ===")

# 7. 发布技能包
pkg = api("POST", "/governance/marketplace/packages",
          {"company_id": CID},
          {"package_type": "SKILL",
           "key": "policy-verification-" + str(int(time.time())),
           "name": "政策核查技能包", "author": "内部研发",
           "permissions_required": ["knowledge.read", "web.search"],
           "dependencies": [], "manifest": {"version": 1}})
check("发布技能包", pkg.get("id"))

# 8. 安装需确认权限
r = api("POST", f"/governance/marketplace/packages/{pkg['id']}/install?company_id={CID}")
check("安装前显示权限清单", r.get("installed") is False and
      r.get("requires_confirmation", {}).get("permissions_required"))

# 9. 确认后安装 + 回滚
inst = api("POST",
           f"/governance/marketplace/packages/{pkg['id']}/install?company_id={CID}&confirm_permissions=true")
check("确认权限后安装成功", inst.get("installed") is True)
rb2 = api("POST", f"/governance/installations/{inst['installation_id']}/rollback")
check("安装回滚 OK", rb2.get("rolled_back") is True)

print("\n=== P3-15 Enterprise Templates ===")

# 10. 模板列表（4个内置）
tpls = api("GET", "/governance/templates")
keys = {t["key"] for t in tpls}
check("4个内置模板", {"general", "project", "creative", "research"} <= keys)

# 11. 一键创建公司结构
cc = api("POST", "/governance/create-company-from-template",
         {"owner_company_id": CID}, {"template_key": "creative",
                                     "company_name": "云端创意工作室"})
check("模板创建公司结构", cc.get("status") == "SUCCESS"
      and len(cc.get("created_agents", [])) >= 4)
print(f"     创建了: {[a['name'] for a in cc['created_agents']]}")

print("\n=== P3-16 Autonomous Company Mode ===")

# 12. 设置 L3
s3 = api("PUT", "/governance/autonomous-settings?company_id=" + CID,
         {}, {"level": 3})
check("设置 L3", s3.get("level") == 3)

# 13. L4 无限制被拒
r = api("PUT", "/governance/autonomous-settings?company_id=" + CID,
        {}, {"level": 4})
check("L4必须设限制", r.get("__http_error__") == 400)
s4 = api("PUT", "/governance/autonomous-settings?company_id=" + CID,
         {}, {"level": 4, "cost_limit": 50.0, "action_limit": 100})
check("L4+限制可设置", s4.get("level") == 4)

# 14. 自治执行日志
lg = api("POST", "/governance/autonomous-executions",
         {"company_id": CID},
         {"level": 4, "why": "每日例行监测", "plan": {"steps": 3},
          "actions_taken": ["搜索", "分析"], "result": {"found": 2},
          "cost_estimate": 0.5})
check("自治执行审计日志", lg.get("id"))

# 15. 全局紧急停止（带检查点）
cp = {"paused_workflows": ["wf-1"], "pending_tasks": 3}
es = api("POST", "/governance/emergency-stop",
         {"company_id": CID}, {"checkpoint": cp})
check("紧急停止+检查点保存", es.get("checkpoint_saved") is True)

# 16. 停止期间执行被拒 (423)
r = api("POST", "/governance/autonomous-executions",
        {"company_id": CID}, {"level": 4, "why": "should fail"})
check("停止期间禁止自治执行", r.get("__http_error__") == 423)

# 17. 从检查点恢复
rs = api("POST", f"/governance/resume-from-checkpoint?company_id={CID}")
check("从Checkpoint恢复", rs.get("resumed", 0) >= 1
      and rs.get("restored_checkpoint") == cp)

# 恢复默认 L2
api("PUT", "/governance/autonomous-settings?company_id=" + CID,
    {}, {"level": 2})

print(f"\n=== P3-13~16 结果: {PASS} 通过 / {FAIL} 失败 ===")
assert FAIL == 0