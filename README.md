# AI-EOS 人工智能企业操作系统

> 数字员工工作平台 —— 创建 AI 公司、雇佣数字员工、下达目标，系统自动拆解任务、多 Agent 并行执行、汇总成果。
>
> **Phase 1 MVP 已交付**（2026-08-23）

## 文档导航

| 文档 | 内容 |
|------|------|
| [INTRODUCTION.md](INTRODUCTION.md) | 系统介绍：产品能力总览（从这里开始了解） |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 开发文档：架构/目录/数据模型/API/模块实现/踩坑记录 |
| [AI_EOS_DEVELOPMENT_CONSTITUTION.md](AI_EOS_DEVELOPMENT_CONSTITUTION.md) | 开发宪法：八条强制规则 |
| docs/ | 详细设计文档（架构、Schema、API、Runtime、测试报告等 11 份） |
| docs/development/MASTER_TASK_BOARD.md | Phase 1 任务看板（16 项全部 COMPLETED） |

## 技术栈

- 前端: Next.js 14 + TypeScript + Tailwind CSS
- 后端: Python FastAPI + SQLAlchemy (async) + Alembic
- 数据库: PostgreSQL 16 (Docker) + Redis 7 (预留)
- 模型网关: Provider Adapter 插件化（OpenAI 兼容已实装 / Anthropic / Gemini 骨架）

## 快速启动

```bash
# 1. 启动基础设施
docker compose -f docker/docker-compose.yml up -d

# 2. 后端
cd apps/api && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000 --reload

# 3. 前端
cd apps/web && npm install && npm run dev   # http://localhost:3000
```

测试账号：`boss@aieos.dev` / `Passw0rd!`

## 路线图

- ✅ **Phase 1**：AI 公司基础系统（组织/Agent/模型/技能/任务/协同/资产/管控）
- 🔜 **Phase 2**：AI 会议系统、工作流引擎、任务依赖 DAG、全过程回放
- 🔮 **Phase 3**：Agent 学习成长、绩效体系、市场生态、Computer Agent
