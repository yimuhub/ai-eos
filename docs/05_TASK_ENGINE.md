# Task Engine

## 状态机 TRANSITIONS

见 docs/02_DATABASE_SCHEMA.md。所有变更经 task_engine.transition()：
校验合法性 -> 更新 status -> 写 task_events -> 写 event_log。
非法跳转抛 IllegalTransition -> HTTP 409。

## 后台执行

- 单 Agent: POST /tasks/{id}/start -> background_jobs.submit_async
- 多 Agent: POST /orchestrate -> Coordinator 编排

## Orchestrator 流程

1. DECOMPOSE_PROMPT: CEO LLM 输出 JSON {subtasks:[{title,description,agent_name}]}
2. 子任务持久化（created_by 继承父任务的用户，满足外键约束）
3. asyncio.gather 并行 run_one（每 worker 独立 session/engine）
4. worker 返回纯 dict {task,agent,status,output}——规避跨 session ORM 缓存问题
5. SUMMARIZE_PROMPT: CEO 汇总为 Markdown 报告
6. 失败子任务附于报告尾部，不阻塞整体交付
