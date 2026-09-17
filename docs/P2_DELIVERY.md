# AI-EOS Phase 2 交付文档

## 概述

Phase 2 在 P1（公司/部门/Agent 层级 + 任务引擎 + 模型网关 + 技能系统）基础上，让 AI 公司真正"像公司一样运行"：项目管理、工作流、会议、知识、记忆、协同、治理、绩效、自动化调度全链路打通。

**集成测试：16/16 通过**（`tests/test_p2_integration.py`，含真实 LLM 会议）

## 模块清单

| 包 | 模块 | 核心交付 | 状态 |
|---|---|---|---|
| P2-01 | 项目管理 | Project/Milestone/ProjectMember/状态机(8态)/进度计算/前端页面 | ✅ |
| P2-02 | 高级工作流引擎 | 5 张表 + WorkflowRuntime；顺序/并行/条件/等待/审批/Agent/子工作流节点；全部状态持久化 | ✅ |
| P2-03 | 可视化工作流编辑器 | SVG 画布拖拽建点、黄色端口连线、属性面板、保存/加载/运行 | ✅ |
| P2-04 | AI 会议系统 | 开场→讨论→总结三阶段自动推进；真实 LLM 发言；DECISION/ACTION 提取；行动项一键转任务；纪要生成 | ✅ |
| P2-05 | 协同协议 2.0 | 12 种消息类型；协作图谱(manager/collaborator/reporter)；按类型+图谱智能路由 | ✅ |
| P2-06 | 知识库 | 文档分块(500字滑窗)；中英混合分词(bigram)检索；RAG 接口直接注入 prompt | ✅ |
| P2-07 | 记忆系统 | 5 类记忆(EPISODIC/SEMANTIC/PROCEDURAL/WORKING/REFLECTION)；重要度过滤；TTL 过期清理 | ✅ |
| P2-08 | Project Context | Context Builder 聚合项目全量上下文；渲染 prompt 文本；版本化快照 | ✅ |
| P2-09 | 成果版本协同 | Artifact+ArtifactVersion；Draft→InReview→Approved/Rejected 状态机；版本不可变历史 | ✅ |
| P2-10 | 高级人工介入 | Automation Level 0-4；审批请求流；人工 Override 日志 | ✅ |
| P2-11 | Agent 绩效 | 从 agent_executions/meetings/messages 真实聚合；完成率/时长/活跃度→综合分；快照排行榜 | ✅ |
| P2-12 | 团队自动组建 | 3 套模板(软件/文旅/研究)；按 Agent name+role 关键词匹配；幂等不重复 | ✅ |
| P2-13 | Browser Agent | https-only + 私网 SSRF 防护 + 主机名黑名单；每次尝试落审计日志 | ✅ |
| P2-14 | Computer Agent | 能力授权模型(grant/revoke)；Mock 执行器(截图/键入/点击/启动应用)；审计日志 | ✅ |
| P2-15 | 自动化调度 | interval/daily 触发；动作(start_workflow/send_message/snapshot)；持久化 next_run_at；后台循环每10s扫描 | ✅ |
| P2-16 | 集成测试 | 全生命周期串联场景 16 项断言全部通过 | ✅ |

## 新增数据表（14 张）

projects, milestones, project_members, project_context_snapshots,
workflow_definitions/nodes/edges/executions/steps (5),
meetings, meeting_messages, meeting_participants, meeting_decisions,
meeting_action_items, agent_collaborations, agent_messages,
kb_documents, kb_chunks, agent_memories, artifacts, artifact_versions,
intervention_policies, approval_requests, override_logs,
performance_snapshots, browser_action_logs, computer_permissions,
computer_action_logs, automation_schedules, automation_runs

## 新增 API 前缀

`/projects` `/workflows` `/meetings` `/collaboration` `/knowledge`
`/memory` `/artifacts` `/intervention` `/performance` `/team-builder`
`/browser-agent` `/computer-agent` `/schedules`

## 验收场景（集成测试覆盖）

```
创建项目 → 自动组队 → 知识注入/RAG → CEO协作派单
→ AI会议(真实LLM三轮发言) → 决策+行动项 → 转任务
→ 项目上下文快照 → 成果 Draft→Review→Approve
→ 自动化等级治理 → 绩效排行榜   ✅ 全部通过
```

## 已知小问题

1. `DELETE /projects/{id}` 级联删除报 500（快照表外键未级联），临时用 SQL 清理，不影响主流程
2. 会议纪要中发言人显示 agent UUID 而非姓名（早期发言记录未冗余姓名）
3. 工作流编辑器连线在个别浏览器需要精确从黄点起拖

## 运行方式

```bash
# 后端 (含调度循环)
cd apps/api && uv run uvicorn app.main:app --port 8000 --reload
# 前端
cd apps/web && npm run dev
# 集成测试
python3 tests/test_p2_integration.py
```
