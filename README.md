# AI-EOS 人工智能企业操作系统

> 数字员工工作平台 —— 创建 AI 公司、雇佣数字员工、下达目标，系统自动拆解任务、多 Agent 并行执行、汇总成果。

## 当前状态

代码已实现到 **Phase 3**（Agent 学习成长 / 绩效 / 职业发展 / Computer Agent 均已落地），
**不是** Phase 1 MVP。文档此前长期停留在 Phase 1，本文件已按实测数据校正。

| 项目 | 实测值 |
|------|--------|
| 后端 | 21,554 行 Python / 135 个文件 |
| 前端 | 17,738 行 TS/TSX / 34 个一级业务页面 |
| API | **307 条路径 / 354 个端点** |
| 数据表 | **94 张** |
| 数据库迁移 | 28 个 Alembic 版本 |
| 测试 | 113 个用例（离线可重复） |

> **完成度说明**：功能广度已经很大，但工程质量刚完成一轮系统性补齐
> （修复 9 处未定义变量、28 处静默吞异常、11 处接口契约错配，并重建测试体系与交付门禁）。
> 详见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)。

## 文档导航

| 文档 | 内容 |
|------|------|
| [INTRODUCTION.md](INTRODUCTION.md) | 系统介绍：产品能力总览（从这里开始了解） |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 开发文档：架构 / 目录 / 数据模型 / API / 模块实现 / 踩坑记录 |
| [UPGRADE_NOTES.md](UPGRADE_NOTES.md) | **工程质量改造记录**：改了什么、为什么改、如何验证 |
| [AI_EOS_DEVELOPMENT_CONSTITUTION.md](AI_EOS_DEVELOPMENT_CONSTITUTION.md) | 开发宪法：八条强制规则 |
| docs/ | 详细设计文档（架构 / Schema / API / Runtime / 测试报告等） |

## 技术栈

- **前端**：Next.js 14（App Router）+ TypeScript（strict）+ Tailwind CSS
- **后端**：Python + FastAPI + SQLAlchemy 2.x（async）+ Alembic
- **数据库**：PostgreSQL 16（Docker）；测试用 SQLite（`aiosqlite`），无需外部依赖
- **缓存**：Redis 7（已部署，Phase 2 启用）
- **模型网关**：Provider Adapter 插件化（OpenAI 兼容 / Anthropic / Gemini）

## 快速启动

### 前置要求

- Python 3.11+、Node.js 18+
- Docker（用于 PostgreSQL）

### 1. 启动数据库

```bash
docker compose -f docker/docker-compose.yml up -d
```

### 2. 后端

```bash
cd apps/api

# 安装依赖
uv sync                     # 或：python -m venv .venv && .venv/bin/pip install -e .

# 配置环境变量（必须！见下方说明）
cp ../../.env.example .env
# 编辑 apps/api/.env，至少填好 DATABASE_URL / JWT_SECRET / 模型密钥

# 建表并启动
uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000 --reload
```

> **关于密钥**：模型密钥写在 `apps/api/.env`（键名如 `OPENAI_COMPAT_API_KEY`）。
> 改造前的代码只读系统环境变量、从不加载 `.env`，会让密钥取到空串并报 401；
> 现已修复为统一从 `.env` 加载。注意**真实系统环境变量的优先级更高**——
> 这是有意设计，避免生产环境被仓库里的文件误改。

### 3. 前端

```bash
cd apps/web
npm install
npm run dev          # http://localhost:3000
```

### 4. 创建账号

**没有预置账号**，请直接在登录页注册（用户名 ≥ 2 位，密码 ≥ 8 位且需同时含字母和数字）。

> 旧版 README 让用 `boss@aieos.dev` / `Passw0rd!` 登录 —— 那套账号在整个代码库中
> 从未被创建，属文档层面的历史遗留，现已删除该说明。

首次登录后会看到**三步引导向导**：创建 AI 公司 → 配置模型 → 生成团队。

### 5. 界面使用要点

系统有 35 个页面。**不用在侧边栏里逐级找**：

| 快捷键 | 作用 |
| --- | --- |
| `⌘K` / `Ctrl+K` | 打开全局搜索，按标题、说明、英文名或口语说法直达任意页面 |
| `↑` `↓` | 在搜索结果中移动选择 |
| `↵` | 打开选中项 |
| `Esc` | 关闭弹窗 |

侧边栏按使用动线分为五组（指挥中枢 / 组织与智能体 / 协作执行 / 知识与资产 / 成长与治理），
首次进入只展开你当前所在的分组；鼠标悬停任意菜单或分组可看到它"为什么存在"的一句话说明。

所有破坏性操作都会弹出一致的确认框，并**逐条列出会失去什么**；
其中不可恢复的操作（删除公司、批量删除员工/任务、删除项目）需要逐字输入确认词
——这是"手滑"和"确认"之间的真实区别。

## 验证与测试

### 交付前自检（推荐）

一条命令跑完全部检查，不需要启动服务、不需要模型密钥：

```bash
./tools/check.sh
```

覆盖：后端静态检查（未定义名 / 语法）→ 应用与 OpenAPI 可生成 → SQLAlchemy mapper 可配置 →
前后端接口契约（404 / 405 门禁）→ 前端 TypeScript strict 类型检查 → 前端 ESLint → 测试套件。
**任一失败即非零退出**，可直接接进 CI。

### 分别运行

```bash
# 后端测试（122 个用例，每会话独立临时 SQLite，不碰真实库）
cd apps/api && .venv/bin/python -m pytest tests/ -q

# 前端类型检查与 lint
cd apps/web && npx tsc --noEmit && npx eslint app components lib

# 前后端接口契约（独立运行，便于定位具体错配）
cd apps/api && .venv/bin/python ../../tools/check_api_contract.py --strict
```

> 测试里除了功能用例，还有一类**体验护栏**（`tests/test_code_quality.py`）：
> 禁止原生 `confirm/alert`、要求每个页面都有入口、导航不得有死链。
> 这类问题编译器和 lint 都拦不住，但会实实在在地让产品显得不专业。

> 仓库根目录的 `tests/` 是**历史手工脚本**，依赖 `/tmp` 临时文件与硬编码 UUID，无法在
> pytest 下运行。它们已从测试收集中排除，说明见 [tests/LEGACY_SCRIPTS.md](tests/LEGACY_SCRIPTS.md)。
> 当前测试套件位于 `apps/api/tests/`。

## 路线图

- ✅ **Phase 1**：AI 公司基础系统（组织 / Agent / 模型 / 技能 / 任务 / 协同 / 资产 / 管控）
- ✅ **Phase 2**：AI 会议系统、工作流引擎、任务依赖 DAG、全过程回放
- ✅ **Phase 3**：Agent 学习成长、绩效体系、职业发展、Computer Agent
- 🚧 **工程质量**：可靠性改造已完成一轮（见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)）；
  下一步是收敛功能广度、精细化权限模型、建立度量体系
