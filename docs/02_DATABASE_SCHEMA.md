# 数据库 Schema（16 张表）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| users | 用户账号 | email(unique), password_hash(bcrypt) |
| companies | 公司 | owner_id→users（数据隔离锚点） |
| departments | 部门树 | parent_department_id 自引用层级 |
| agents | 数字员工 | status(6态), system_prompt, manager_agent_id |
| models | LLM Provider 配置 | provider_type(enum), base_url, api_key_env, model_id |
| agent_model_configs | 模型绑定 | role: primary/secondary/fallback（故障自动切换顺序） |
| skills | Skill 清单 | slug(unique), input/output_schema, permissions |
| agent_skills | 安装关系 | uq(agent_id, skill_id) |
| tasks | 任务 | parent_task_id 自引用(子任务), status(9态), priority(4级) |
| task_events | 状态机日志 | event_type, actor_type, payload(JSON) |
| agent_executions | 运行记录 | input/output/error/metadata(token用量、所用模型) |
| execution_checkpoints | 断点 | checkpoint_type(planning/execution/result), context_snapshot(JSON) |
| interventions | 人工介入 | type(6种: MESSAGE/INSTRUCTION/FILE/URL/PRIORITY_CHANGE/AGENT_CHANGE) |
| conversations / messages | 协作消息 | sender_type(agent/user/system), message_type(7种) |
| assets | 成果资产 | type(6种), version, task_id+agent_id 全程溯源 |
| event_log | 全局事件流 | event_type/entity_type/entity_id/actor/payload |

## 任务状态机

```
TODO → PLANNING → RUNNING → REVIEW → COMPLETED
         │           │ ├→ WAITING ─┤
         │           │ └→ PAUSED ──→ RUNNING / CANCELLED
         └───────────┴→ CANCELLED
FAILED → PLANNING (重试)
COMPLETED 为终态，禁止任何跳出（非法跳转返回 409）
```

迁移命令：`uv run alembic revision --autogenerate -m "..." && uv run alembic upgrade head`
版本基线：`ed17a07daca5_phase1_core_schema`
