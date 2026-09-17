"""P2-16 Integration Test: 全模块串联场景。

场景（对应任务书 Phase 2 验收）：
1. 创建项目「新文旅项目」
2. Team Builder 自动组建团队
3. 知识库注入文旅知识 → RAG 上下文可用
4. CEO 发起协作消息（TASK_ASSIGNED 路由到团队）
5. 召开战略会议 → 决策/行动项 → 转任务
6. 项目上下文快照 v1/v2（会议前后对比）
7. 成果版本协同 Draft→Review→Approve
8. Automation Level 检查 + 绩效快照
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"
STRAT = "d7e8607b-95a7-400f-b8ac-e83830d3e76f"
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=" * 50)
print("P2-16 集成测试：AI 项目全生命周期")
print("=" * 50)

# ---- 1. 创建项目 ----
print("\n[场景1] 创建项目")
proj = api("POST", "/projects", {"company_id": CID},
           {"name": "云梦山文旅综合开发项目",
            "description": "含景区升级、民宿集群、夜游经济的综合开发"})
pid = proj["id"]
check("项目创建", bool(pid))

# ---- 2. 自动组建团队 ----
print("\n[场景2] 团队自动组建")
tb = api("POST", f"/team-builder/build/{pid}", {"company_id": CID}, {})
check("匹配文旅模板", tb.get("template") == "文旅策划团队")
roles = {a["role_in_project"] for a in tb.get("added", [])}
check("总监/策划/市场就位", {"项目总监", "策划师"} <= roles and
      any("市场" in r for r in roles), str(roles))

# ---- 3. 知识库注入 + RAG ----
print("\n[场景3] 知识库")
kb = api("POST", "/knowledge/documents", {"company_id": CID},
         {"title": "云梦山资源盘点", "project_id": pid,
          "content": "云梦山位于豫北，主峰海拔589米，有战国古军营遗址、云端草原两大核心资源。"
                     "区位距京港澳高速出口18公里。客源以周边2小时自驾圈为主，周末占比72%。"})
check("知识入库", kb.get("status") == "READY" and kb.get("chunk_count") >= 1)
rag = api("GET", "/knowledge/search",
          {"company_id": CID, "q": "云梦山 核心资源", "top_k": 2})
check("RAG检索命中", bool(rag.get("results")))

# ---- 4. 协作消息路由 ----
print("\n[场景4] 协作协议")
msg = api("POST", "/collaboration/messages", {"company_id": CID},
          {"sender_agent_id": CEO, "message_type": "TASK_ASSIGNED",
           "title": "启动云梦山项目策划",
           "content": "请各位按分工启动前期工作",
           "context_ref": pid, "context_type": "project"})
receivers = msg.get("receiver_agent_ids") or []
check("任务指派路由到团队", len(receivers) >= 2 and STRAT in receivers or MKT in receivers,
      str([r[:8] for r in receivers]))

# ---- 5. 战略会议（真实 LLM）----
print("\n[场景5] AI 会议")
meet = api("POST", "/meetings", {"company_id": CID},
           {"company_id": CID, "project_id": pid,
            "title": "云梦山项目定位会",
            "topic": "讨论云梦山项目的市场定位与首发产品",
            "context_material": "核心资源：战国古军营遗址、云端草原；客源：周边自驾亲子家庭",
            "meeting_type": "STRATEGY_MEETING",
            "participant_ids": [CEO, STRAT, MKT]})
mid = meet["id"]
api("POST", f"/meetings/{mid}/start", {})
final = {}
for _ in range(40):
    time.sleep(10)
    final = api("GET", f"/meetings/{mid}")
    if final.get("status") == "COMPLETED":
        break
check("会议完成（真实LLM发言）", final.get("status") == "COMPLETED",
      final.get("status"))
check("产生决策", len(final.get("decisions", [])) >= 1)
check("产生行动项", len(final.get("action_items", [])) >= 1)

conv = api("POST", f"/meetings/{mid}/action-items/convert", {},
           {"project_id": pid})
check("行动项转任务", len(conv.get("converted_task_ids", [])) >= 1)

# ---- 6. 项目上下文快照 ----
print("\n[场景6] Project Context")
ctx = api("GET", f"/projects/{pid}/context", {"company_id": CID})
check("上下文聚合(里程碑+会议+任务)",
      ctx.get("milestones") is not None and len(ctx.get("meetings", [])) >= 1
      and len(ctx.get("tasks", [])) >= 1)
snap = api("POST", f"/projects/{pid}/context/snapshots", {"company_id": CID},
           {"summary": "会议后快照"})
check("快照版本化", snap.get("version") == 1)

# ---- 7. 成果版本协同 ----
print("\n[场景7] 成果协同")
art = api("POST", "/artifacts", {"company_id": CID},
          {"name": "云梦山策划方案", "content": "v1 框架稿", "project_id": pid})
aid = art["id"]
r1 = api("POST", f"/artifacts/{aid}/review", {"company_id": CID},
         {"decision": "approve"})
check("DRAFT直接批准被状态机拦截", r1.get("__http_error__") == 409)
api("POST", f"/artifacts/{aid}/submit", {"company_id": CID},
    {"content": "v2 完整方案", "submitted_by_agent_id": STRAT})
r2 = api("POST", f"/artifacts/{aid}/review", {"company_id": CID},
         {"decision": "approve", "reviewer_agent_id": CEO})
check("Submit→Approve 流转", r2.get("status") == "APPROVED")

# ---- 8. 治理：自动化等级 + 绩效 ----
print("\n[场景8] 治理与绩效")
pol = api("PUT", "/intervention/policy", {"company_id": CID}, {"level": 2})
c = api("POST", "/intervention/check",
        {"company_id": CID, "action_type": "artifact_approve"})
check("L2 下 artifact_approve 需审批", c.get("required") is True)
perf = api("POST", f"/performance/company/{CID}/snapshot")
check("绩效快照生成", len(perf.get("ranked", [])) == 4)

# ---- 清理 ----
api("DELETE", f"/knowledge/documents/{kb['id']}")
for s in api("GET", "/schedules", {"company_id": CID}):
    pass  # schedules 无关

print("\n" + "=" * 50)
print(f"集成测试结果: {PASS} 通过 / {FAIL} 失败")
print("=" * 50)
if FAIL == 0:
    print("🎉 P2 全生命周期验收通过")
