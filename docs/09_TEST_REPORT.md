# Phase 1 测试报告

## 端到端验收（P1-16 十二场景）

| # | 场景 | 结果 |
|---|------|------|
| 01 | 创建公司「AI 未来公司」 | PASS |
| 02 | 创建 战略部/市场部/技术部（+创意中心） | PASS |
| 03 | 创建 CEO/战略分析师/市场研究员/程序员 4 个 Agent | PASS |
| 04 | 配置模型(agnes-2.5-flash 实测连通)+技能+部门+岗位 | PASS |
| 05 | 创建任务：研究 AI 企业操作系统市场 | PASS |
| 06 | CEO 分析并拆分子任务（LLM 输出 JSON 持久化） | PASS（3-4 个子任务） |
| 07 | 多 Agent 并行执行各自子任务并 COMPLETED | PASS |
| 08 | Agent 通信：协作会话中老板发言→Agent 自动回复 | PASS（含观点差异） |
| 09 | CEO 汇总生成 3000 字 Final Report | PASS |
| 10 | 报告自动进入 Asset Center | PASS |
| 11 | 用户查看全过程（任务/事件流/日志/资产） | PASS |
| 12 | 状态持久化：暂停/取消/服务重启后状态保留 | PASS |

## 状态机测试 (tests/test_state_machine_e2e.py)

- TODO→PAUSED 非法跳转被拒 (409 IllegalTransition) PASS
- PAUSED→RUNNING→CANCELLED 合法链路 + 取消原因记录 PASS

## 前端

- npm run build 通过（Next.js 14, 10 路由）
- 登录/指挥中心/Agent/任务/资产/日志/协作 全页面人工+自动化验证
- 界面全面中文化

## 已修复的关键 Bug（记录供 Phase 2 参考）

1. passlib+bcrypt5 不兼容 -> 锁 bcrypt<4
2. asyncpg 跨事件循环 -> background_jobs 独立 engine
3. orchestrator created_by 外键误用 agent id -> 继承父任务用户
4. worker 结果收集的跨 session ORM 缓存 -> 纯 dict 返回
