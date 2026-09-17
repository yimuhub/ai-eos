# Agent Runtime

## 执行流程 (run_agent_on_task)

```
1. 创建 AgentExecution(status=running)      # 运行记录
2. agent.status = RUNNING                    # 员工状态
3. TODO/PLANNING → RUNNING 状态迁移          # 合法性由 TRANSITIONS 校验
4. build_context:
   - system_prompt (人格+岗位)
   - 已安装技能清单注入 system 消息
   - 任务 title/description 作为 user 消息
   - 模型优先级 primary→secondary→fallback
5. 写 planning checkpoint（完整 messages 快照）
6. ModelGateway.chat_with_agent_models       # 主模型失败自动切换备选
7. execution.output = 结果; metadata 记录 token 用量与所用模型
8. 写 result checkpoint
9. 成果自动保存为 Asset(.md)，asset_id 回写 execution.metadata
10. 异常路径: execution.status=failed + error + event_log
```

## Pause/Resume

- pause_execution: status=paused + execution checkpoint(step=2)
- resume_execution: 读取最新 checkpoint，status=running
- 服务重启后状态在数据库中保留（Rule 5），可从 checkpoint 恢复

## 会话自动回复 (_agents_reply)

用户在协作会话发言后，参与者 Agent 依次读取会话历史，
以自身身份生成 100 字以内回应（最多 3 个 Agent，控制成本）。\n