# Phase 2 计划：AI 公司协同与自动化

按总任务书第二期范围：

- P2-01 AI 会议系统（每日站会/评审会/CEO 决策会，会议纪要自动转任务）
  —— 基础已在协作会话的 _agents_reply 打好，扩展为结构化会议编排
- P2-02 工作流引擎（可视化拖拽、条件分支、并行网关、人工审批节点）
- P2-03 Agent 通信协议升级（TASK_REQUEST/TASK_RESPONSE 结构化路由）
- P2-04 任务依赖 DAG（A 完成->B 开始），基于 parent_task_id 扩展依赖表
- P2-05 并行规模提升（>5 Agent、跨部门动态组队）
- P2-06 Human in the Loop 强化（审批卡点、方向修正后重新规划）
- P2-07 资产版本控制（asset_versions 表，V1/V2/Final-Approved）
- P2-08 全过程回放（基于 event_log 的时间轴回放器）
- 基建：Redis 缓存启用、SSE/WebSocket 实时推送替换前端轮询、
  web_search 接真实后端、Celery/Temporal 替换内置执行器
