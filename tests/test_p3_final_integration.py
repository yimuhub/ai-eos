"""P3-17 Final Integration Test: AI 数字员工全生命周期。

场景（对应任务书最终验收）：
新项目 → Factory 创建新 Agent → Training → Skill 评估升级
→ 加入项目工作 → 会议 → Reflection → Improvement Proposal
→ Sandbox Test → 用户批准 → Skill/Agent 升级部署
→ Marketplace 安装技能包 → Autonomous 治理 + Emergency Stop + Checkpoint 恢复
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()
CEO = "c1250aba-2f84-4a50-959c-e387a6ef23b4"
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "detail": e.read().decode()[:200]}


print("=" * 55)
print("P3-17 Final Integration：数字员工成长与全自动生产力")
print("=" * 55)

# ---- 阶段A: 新项目 + 团队 ----
print("\n[阶段A] 项目启动")
proj = api("POST", "/projects", {"company_id": CID},
           {"name": "齐云山文旅夜游经济项目", "description": "夜游产品策划与运营方案"})
pid = proj["id"]
check("1. 新项目创建", bool(pid))

tb = api("POST", f"/team-builder/build/{pid}", {"company_id": CID}, {})
check("2. 自动组建团队", tb.get("team_size", 0) >= 3)

# ---- 阶段B: Agent Factory 创建新 Agent ----
print("\n[阶段B] 发现能力缺口 → Agent Factory")
bp = api("POST", "/factory/blueprint", {"company_id": CID},
         {"goal": "项目需要一个短视频编导，负责抖音夜游内容的脚本与拍摄统筹"})
check("3. 能力缺口触发蓝图生成", bp.get("status") == "BLUEPRINT")
job = bp.get("job_id")

ra = api("POST", f"/factory/jobs/{job}/approve",
         {"company_id": CID}, {})
new_agent_id = ra.get("agent_id")
check("4. 用户批准后创建新Agent(测试期)", new_agent_id and ra.get("status") == "TESTING")

rt = api("POST", f"/factory/jobs/{job}/test-result",
         {"passed": True, "score": 85, "notes": "试拍脚本通过评审"})
check("5. 测试通过 → 上岗ACTIVE", rt.get("status") == "ACTIVE")

# ---- 阶段C: 培训新 Agent ----
print("\n[阶段C] Agent Training")
tr = api("POST", "/training/sessions", {"agent_id": new_agent_id},
         {"training_type": "CONVERSATION",
          "title": "夜游内容创作规范",
          "raw_input": "夜游短视频黄金时长15秒；前3秒必须出现灯光夜景画面；"
                       "文案必须包含地点话题标签；发布时间固定在19:00-21:00。"})
check("6. 对话训练+LLM提取学习点", tr.get("status") == "EXTRACTED")
app_r = api("POST", f"/training/sessions/{tr['id']}/approve")
check("7. 用户批准训练生效", app_r.get("applied_count", 0) >= 1)

# 查看学到了什么
detail = api("GET", f"/training/sessions/{tr['id']}")
check("8. 可查看Agent学到什么", len(detail.get("learning_points", [])) >= 1)

# ---- 阶段D: 技能成长（证据驱动） ----
print("\n[阶段D] Skill Growth")
nodes = api("GET", "/skill-growth/tree", {"company_id": CID})
node_map = {n["key"]: n["id"] for n in nodes}
if "basic_research" not in node_map:
    nb = api("POST", "/skill-growth/nodes", {}, {"key": "basic_research", "name": "基础研究"})
    node_map["basic_research"] = nb["id"]

acq = api("POST", f"/skill-growth/agents/{new_agent_id}/acquire",
          None,
          {"skill_node_id": node_map["basic_research"],
           "source_type": "training",
           "evidence": [{"type": "training", "ref": tr["id"], "note": "培训获得"}]})
print("     acquire resp:", str(acq)[:150])
check("9. 学习获得新技能(BEGINNER)", acq.get("level") == "BEGINNER")
asn = acq["id"]
for i in range(2):
    api("POST", f"/skill-growth/agent-skills/{asn}/evidence", {},
        {"evidence_type": "task", "note": f"实战任务{i+1}"})
up = api("POST", f"/skill-growth/agent-skills/{asn}/upgrade", {},
         {"new_level": "JUNIOR", "score": 82, "method": "task_review",
          "evidence_summary": "两个实际拍摄任务通过"})
check("10. 有证据升级 JUNIOR", up.get("level") == "JUNIOR")
mx = api("GET", f"/skill-growth/agents/{new_agent_id}/matrix")
row = [m for m in mx if m["evidence_count"] >= 1]
check("11. Skill Matrix 含证据", len(row) >= 1)

# ---- 阶段E: 实战工作 + 会议 ----
print("\n[阶段E] 协同工作")
msg = api("POST", "/collaboration/messages", {"company_id": CID},
          {"sender_agent_id": CEO, "message_type": "TASK_ASSIGNED",
           "title": "夜游内容首拍任务", "content": "请按规范执行首次拍摄",
           "context_ref": pid, "context_type": "project"})
check("12. 任务派发到团队", bool(msg.get("id")))

meet = api("POST", "/meetings", {"company_id": CID},
           {"company_id": CID, "project_id": pid,
            "title": "夜游首拍复盘会",
            "topic": "讨论首次夜游拍摄的效果与改进方向",
            "meeting_type": "STRATEGY_MEETING",
            "participant_ids": [CEO, MKT, new_agent_id]})
mid = meet["id"]
api("POST", f"/meetings/{mid}/start", {})
final = {}
for _ in range(40):
    time.sleep(10)
    final = api("GET", f"/meetings/{mid}")
    if final.get("status") == "COMPLETED":
        break
check("13. 自动召开会议完成(LLM)", final.get("status") == "COMPLETED",
      final.get("status"))
check("14. 会议产生行动项", len(final.get("action_items", [])) >= 1)
conv = api("POST", f"/meetings/{mid}/action-items/convert",
           {"project_id": pid})
check("15. 行动项自动转任务", len(conv.get("converted_task_ids", [])) >= 1)

# ---- 阶段F: Reflection → Governance → 升级部署 ----
print("\n[阶段F] 复盘与治理式自我改进")
refl = api("POST", "/reflection/experiences", {"agent_id": new_agent_id},
           {"agent_id": new_agent_id,
            "title": "夜游首拍执行",
            "outcome": "PARTIAL",
            "summary": "视频完成但发布超时",
            "user_feedback": "发布流程不熟练",
            "execution_output": "成片1条，发布延迟2小时"})
check("16. 项目结束触发Reflection",
      refl.get("reflection_id") and not refl.get("reflect_error"))

props = api("GET", f"/reflection/agents/{new_agent_id}/reflections")
target = [x for x in props if x["id"] == refl.get("reflection_id")]
props_list = target[0]["proposals"] if target else []
check("17. 产出Improvement Proposal",
      len(props_list) >= 1 and props_list[0]["status"] == "PROPOSED")

if props_list:
    gi = api("POST", "/governance/improvements", {"company_id": CID},
             {"agent_id": new_agent_id,
              "reflection_id": refl["reflection_id"],
              "what_improved": props_list[0]["title"],
              "why": "复盘发现发布流程问题",
              "evidence": {"score_before": 60},
              "risk": "低",
              "expected_impact": "发布准时率100%"})
    st = api("POST", f"/governance/improvements/{gi['id']}/sandbox-test"
             f"?test_score=90&baseline_score=70")
    check("18. Sandbox Test 对比", st.get("comparison", {}).get("delta") == 20)
    dep = api("POST", f"/governance/improvements/{gi['id']}/deploy",
              {}, {"approve": True, "note": "提升20分，批准部署"})
    check("19. 用户批准 → 版本化部署", dep.get("status") == "DEPLOYED")
    rb = api("POST", f"/governance/agents/{new_agent_id}/rollback/{dep['rollback_version']}")
    check("20. 支持版本回滚", rb.get("rolled_back_to") == dep["rollback_version"])

# ---- 阶段G: Marketplace + Autonomous ----
print("\n[阶段G] Marketplace 与自治治理")
pkg = api("POST", "/governance/marketplace/packages",
          None,
          {"package_type": "SKILL", "key": f"night-tour-pack-{int(time.time()*1000)}",
           "name": "夜游内容技能包",
           "permissions_required": ["knowledge.read"]})
r = api("POST", f"/governance/marketplace/packages/{pkg['id']}/install?company_id={CID}")
check("21. 安装包显示权限清单", r.get("installed") is False)
inst = api("POST", f"/governance/marketplace/packages/{pkg['id']}/install?company_id={CID}&confirm_permissions=true")
check("22. 确认后安装", inst.get("installed") is True)

s4 = api("PUT", "/governance/autonomous-settings?company_id=" + CID,
         {}, {"level": 4, "cost_limit": 30.0, "action_limit": 50})
check("23. 自治等级 L4(带限制)", s4.get("level") == 4)
lg = api("POST", "/governance/autonomous-executions",
         {"company_id": CID},
         {"level": 4, "why": "夜间自动监测热点", "plan": {"steps": 2},
          "actions_taken": ["搜索"], "result": {"hot_topics": 1},
          "cost_estimate": 0.3})
check("24. 自动运行有审计日志", bool(lg.get("id")))

es = api("POST", "/governance/emergency-stop",
         {"company_id": CID},
         {"checkpoint": {"paused_sessions": 1, "pending_tasks": 2}})
check("25. 全局紧急停止+Checkpoint", es.get("checkpoint_saved") is True)
blocked = api("POST", "/governance/autonomous-executions",
              {"company_id": CID}, {"level": 4, "why": "x"})
check("26. 停止期间接管(禁止执行)", blocked.get("__http_error__") == 423)
rs = api("POST", f"/governance/resume-from-checkpoint?company_id={CID}")
check("27. 从Checkpoint恢复", rs.get("restored_checkpoint") is not None)

api("PUT", "/governance/autonomous-settings?company_id=" + CID,
    {}, {"level": 2})

print("\n" + "=" * 55)
print(f"P3-17 最终集成测试: {PASS} 通过 / {FAIL} 失败")
print("=" * 55)
if FAIL == 0:
    print("🎉 Phase 3 全部验收标准达成")
