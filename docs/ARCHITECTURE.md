# AI-EOS 总系统架构 v1.0

## 一、技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 / FastAPI / SQLAlchemy 2.0 async / asyncpg |
| 数据库 | PostgreSQL 16（Docker/colima），Alembic 迁移 |
| 前端 | Next.js 14 / TypeScript / Tailwind |
| LLM | 模型网关抽象：OpenAI 兼容协议，agnes-2.5-flash（可换任意 Provider） |

## 二、模块地图

```
app/
├── main.py                  # 入口：挂载全部 router + 调度循环 lifespan
├── db/session.py            # 全局 engine + AsyncSessionLocal
├── background_jobs.py       # 后台线程池（独立 loop + 独立 engine）
├── core/config.py           # 配置
├── model_gateway.py         # LLM 网关：primary→secondary→fallback 降级链
│
├── models/                  # SQLAlchemy 模型（必须在 __init__.py 注册才会进迁移!）
├── api/routes/              # HTTP 路由（每模块一个文件，注册进 main.py）
│
├── workflow_runtime.py      # P2 工作流运行时
├── meeting_engine.py        # P2 会议引擎（开场→讨论→总结→提取→转任务）
├── collaboration/routing.py # P2 协同消息路由
├── knowledge_engine.py      # P2 分块+检索+RAG
├── memory_engine.py         # P2 记忆读写/过期清理
├── context_builder.py       # P2 项目上下文聚合
├── performance_engine.py    # P2 绩效聚合
├── team_builder.py          # P2 团队模板组建
├── browser_agent.py         # P3 浏览器代理(安全边界+审计)
├── computer_agent.py        # P3 计算机代理(权限+Mock)
├── scheduler_engine.py      # P2/P3 调度循环(10s扫描)
├── learning_engine.py       # P3 成长档案/Growth Events
├── training_engine.py       # P3 训练提取/应用/回滚
├── reflection_engine.py     # P3 复盘引擎
├── factory_engine.py        # P3 Agent Factory
├── external_agents.py       # P3 外部 Agent Adapter
└── automation_engine.py     # P3 多步自动化执行器
```

## 三、关键架构决策（ADR）

### ADR-1 后台任务独立 engine
asyncpg 连接绑定创建它的 event loop。后台线程必须用
`background_jobs.submit_async()`（内部新建 engine），禁止复用主 loop 的 session。

### ADR-2 轮次推进 = 显式链式调度
会议/自动化等多轮流程，每轮结束必须显式调用下一轮调度
（如 `schedule_next_round`），否则流程会停在第一轮。

### ADR-3 成长只走提案
所有改变 Agent 能力/人格的操作分两级：
- 直接生效：Growth Event 记录（append-only）
- 需审批：Training Apply / Improvement Deploy / Promotion —— 均有 status 状态机

### ADR-4 时间戳统一 naive UTC
DB 列为 `TIMESTAMP WITHOUT TIME ZONE`。写入前必须
`datetime.now(timezone.utc).replace(tzinfo=None)`，否则 asyncpg 报错。

### ADR-5 API Key 不落库
外部连接只存 `auth_env_var`（环境变量名），运行时解析。

### ADR-6 新模型三步曲（高频坑）
新增表：① models/xxx.py 定义 → ② **models/__init__.py 注册** → ③ alembic revision+upgrade。
漏掉第②步 autogenerate 会静默跳过，直到运行时才报表不存在。

### ADR-7 异步查询规范
`result = await db.execute(select(...)); rows = result.scalars().all()`
—— 不存在 `await db.scalars(...).all()` 这种写法。

## 四、数据模型分组

| 域 | 表 |
|---|---|
| 组织 | companies, departments, agents, agent_model_configs, skills, agent_skills |
| 任务 | tasks, agent_executions, execution_checkpoints, interventions |
| 项目 | projects, milestones, project_members, project_context_snapshots |
| 工作流 | workflow_definitions/nodes/edges/executions/steps |
| 会议 | meetings, meeting_messages/participants/decisions/action_items |
| 协同 | agent_collaborations, agent_messages |
| 知识记忆 | kb_documents, kb_chunks, agent_memories |
| 成长(P3) | agent_profiles/growth_events/learning_records/capability_history, training_*, agent_experiences/reflections, experience_lessons, improvement_proposals, skill_nodes, agent_skill_nodes, skill_evaluations, career_positions, agent_career_records, trial_assignments, career_proposals, agent_reviews, review_cycles |
| 工厂/外部 | agent_factory_jobs, external_agent_connections |
| 治理自治 | agent_sandboxes, agent_versions, self_improvement_cases, marketplace_packages, package_installations, enterprise_templates, company_creation_jobs, autonomous_settings, autonomous_execution_logs, intervention_policies, approval_requests, override_logs, performance_snapshots |
| 审计日志 | event_log, browser_action_logs, computer_action_logs/permissions, automation_definitions/run_records |

完整清单见 `docs/DATABASE.md`（由 alembic versions 反推）。

## 五、API 分组

20+ 前缀：/agents /tasks /projects /workflows /meetings /collaboration
/knowledge /memory /artifacts /intervention /performance /team-builder
/browser-agent /computer-agent /schedules /learning /training /reflection
/skill-growth /career /reviews /factory /external-agents /sandbox
/automations /governance

## 六、测试体系

```
tests/
├── test_p301~p316_e2e.py   # 单模块 E2E
├── test_p2_integration.py   # P2 全生命周期 16 断言
└── test_p3_final_integration.py  # P3 全生命周期 27 断言
```

回归命令：
```bash
for t in tests/test_p*.py; do python3 $t || echo "FAIL: $t"; done
```

## 七、已知债务

1. DELETE /projects 已修复但旧数据需手动清理
2. Automation WAITING_APPROVAL 为简化实现（未持久化断点）
3. Computer/Sandbox 执行器 MOCK（Provider 接口已预留）
4. 部分模块缺前端页面（见 FRONTEND_STATUS.md）
