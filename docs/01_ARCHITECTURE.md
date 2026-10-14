# AI-EOS 架构总览

## 六层架构实现

```
┌────────────────────────────────────┐
│ 用户指挥中心 (Next.js Dashboard)    │  apps/web
├────────────────────────────────────┤
│ AI 组织管理层 (Company/Dept/Agent)  │  apps/api routes/companies, agents
├────────────────────────────────────┤
│ 协同调度层 (Task/Orchestration)     │  app/orchestrator.py + task_engine.py
├────────────────────────────────────┤
│ Agent Runtime (Plan/Execute/CP)    │  app/agent_runtime.py
├────────────────────────────────────┤
│ 模型中心 + 技能中心 (插件化)         │  model_gateway.py + skill_system.py
├────────────────────────────────────┤
│ 数据层 (PostgreSQL + 文件存储)      │  16 张表 + storage/artifacts
└────────────────────────────────────┘
```

## 核心设计决策

| 决策 | 实现 | 对应规则 |
|------|------|---------|
| Provider 插件化 | BaseModelProvider 抽象类 + 注册表，OpenAICompat 已实装，Anthropic/Gemini Skeleton 就位 | Rule 3 |
| Agent 独立身份 | agents 表独立行 + agent_model_configs(primary/secondary/fallback) + agent_skills | Rule 4 |
| 任务可恢复 | execution_checkpoints 表（planning/execution/result 三类快照） | Rule 5 |
| 全程留痕 | event_log 统一事件表 + task_events 状态机日志 | Rule 6 |
| Pause/Resume/Cancel | task_engine.TRANSITIONS 状态机 + checkpoint 恢复点 | Rule 7 |
| 统一消息协议 | conversations/messages 表，7 种消息类型 | Rule 8 |
| 后台执行隔离 | background_jobs.submit_async：每任务独立线程+独立 asyncpg 引擎（规避事件循环绑定） | — |

## 关键技术点：跨线程数据库会话

后台 Agent 执行使用独立 engine。asyncpg 连接绑定于创建它的事件循环，
复用主循环连接池会触发 `got Future attached to a different loop`。
见 `app/background_jobs._make_session_factory()`。

## 多 Agent 并行模型

```
Parent Task (PLANNING)
   │ CEO LLM 拆解 → JSON plan（子任务持久化到 tasks 表）
   ▼
asyncio.gather 并行执行 N 个 worker
   │ 每个 worker: 独立 session → run_agent_on_task → COMPLETED
   ▼
收集成果（纯 dict 传递，避免跨 session ORM 访问）
   ▼
CEO LLM 汇总 → Final Report → Asset Center 自动入库
```

失败隔离：单个子任务失败不影响其他 worker，失败清单附在最终报告尾部。
