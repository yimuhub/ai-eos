# AI-EOS 开发文档

> 代码实现至 **Phase 3** ｜ 文档校正日期：2026-09-17
>
> 本文档面向开发者，覆盖系统架构、目录结构、数据模型、API、核心模块实现与运维指南。
>
> **数据口径说明**：本文中的规模数字已于 2026-09-17 按代码实测校正
> （此前长期停留在 Phase 1 的估值：表数 18 → 94，端点数 38 → 354）。
> 工程质量改造记录见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)。

## 目录

1. [技术栈](#1-技术栈)
2. [项目结构](#2-项目结构)
3. [环境搭建与启动](#3-环境搭建与启动)
4. [数据库设计](#4-数据库设计)
5. [后端模块详解](#5-后端模块详解)
6. [API 一览](#6-api-一览)
7. [前端页面](#7-前端页面)
8. [关键实现与踩坑记录](#8-关键实现与踩坑记录)
9. [开发规则](#9-开发规则)
10. [测试](#10-测试)

---

## 1. 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 前端 | Next.js 14 / React / TypeScript / Tailwind CSS | App Router，暗色主题 |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2 (async) + Pydantic v2 | uv 管理依赖 |
| 数据库 | PostgreSQL 16（colima Docker 容器） | Alembic 迁移 |
| 缓存 | Redis 7（已部署，Phase 2 启用） | |
| LLM | OpenAI 兼容协议（实装 agnes-2.5-flash），Anthropic/Gemini 骨架 | Provider 插件化 |
| 认证 | JWT（python-jose）+ bcrypt（锁定 <4 版本） | |

## 2. 项目结构

```
ai-eos/
├── apps/
│   ├── api/                    # FastAPI 后端
│   │   ├── app/
│   │   │   ├── main.py         # 入口，挂载全部路由组（12 组仅为 Phase 1 的规模，现约 60 个路由模块）
│   │   │   ├── core/           # config.py, security.py(JWT/bcrypt),
│   │   │   │                   # logging_config.py(集中日志), env_loader.py(.env 加载),
│   │   │   │                   # sandbox.py(文件访问沙箱)
│   │   │   ├── db/             # base.py(DeclarativeBase), session.py(async engine)
│   │   │   ├── models/         # 94 张表的 ORM 定义
│   │   │   ├── api/
│   │   │   │   ├── deps.py     # get_current_user 依赖
│   │   │   │   └── routes/     # auth/companies/agents/models_api/skills/
│   │   │   │                   # tasks/messages/assets/events/dashboard/orchestrate
│   │   │   ├── agent_runtime.py    # Agent 执行循环 + checkpoint
│   │   │   ├── orchestrator.py     # 多 Agent 协同编排
│   │   │   ├── task_engine.py      # 任务状态机
│   │   │   ├── model_gateway.py    # 模型网关 + Provider 适配器
│   │   │   ├── skill_system.py     # 技能插件注册表
│   │   │   ├── background_jobs.py  # 后台执行器（独立 engine）
│   │   │   └── services/event_system.py  # 统一事件日志
│   │   └── alembic/            # 迁移（基线 ed17a07daca5）
│   └── web/                    # Next.js 前端
│       ├── app/(main)/         # 登录守卫布局 + 8 个页面
│       ├── components/Layout.tsx
│       └── lib/                # api.ts 客户端, i18n.ts 中文映射
├── docs/                       # 架构/API/Schema/测试报告等设计文档
├── plugins/                    # models/skills/providers 预留插件目录
├── storage/                    # uploads/ + artifacts/(Agent 成果自动归档)
├── tools/                      # check.sh(交付前自检门禁) + check_api_contract.py(接口契约校验) + check_response_shape.py(响应形状校验)
├── tests/                      # ⚠️ 历史手工脚本，非 pytest 用例（见 tests/LEGACY_SCRIPTS.md）
├── apps/api/tests/             # ✅ 当前测试套件（113 用例，pytest + httpx.ASGITransport）
├── docker/docker-compose.yml   # PG16 + Redis7（国内镜像源）
└── AI_EOS_DEVELOPMENT_CONSTITUTION.md  # 开发宪法
```

## 3. 环境搭建与启动

### 前置要求
macOS + colima（`brew install lima && colima start`）、uv、Node.js 20+

### 步骤

```bash
# 1. 数据库容器
cd ai-eos && docker compose -f docker/docker-compose.yml up -d

# 2. 后端：安装依赖 + 迁移 + 启动
cd apps/api
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000 --reload

# 3. 前端
cd ../web
npm install
npm run dev        # http://localhost:3000
```

### 环境变量（apps/api/.env）

```ini
DATABASE_URL=postgresql+asyncpg://aieos:aieos_dev@localhost:5432/aieos
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=<生产必须更换>
OPENAI_COMPAT_API_KEY=<LLM 密钥>
OPENAI_COMPAT_BASE_URL=https://api.agnes-ai.cn/v1
OPENAI_COMPAT_MODEL=agnes-2.5-flash
```

模型密钥通过 `models.api_key_env` 字段指向环境变量名，避免明文入库。

## 4. 数据库设计

**94 张表**（实测，`len(Base.metadata.tables)`），28 个 Alembic 迁移版本。

> 下表为 Phase 1 的核心关系子集，用于理解主干结构；完整定义见
> `docs/02_DATABASE_SCHEMA.md` 与 `apps/api/app/models/`。

```
users ──< companies ──< departments（自引用树）
                │
                ├──< agents >── agent_model_configs >── models
                │        └────> agent_skills     >── skills
                ├──< tasks（parent_task_id 自引用 → 子任务树）
                │        ├──< task_events          （状态机日志）
                │        ├──< agent_executions ──< execution_checkpoints
                │        ├──< interventions        （人工介入）
                │        └──< assets               （成果溯源）
                ├──< conversations ──< messages    （协作消息）
                └──< event_log                     （全局事件流）
```

### 任务状态机（task_engine.TRANSITIONS）

```
TODO → PLANNING → RUNNING → REVIEW → COMPLETED(终态)
                    ├──→ WAITING ──→ RUNNING
                    └──→ PAUSED ───→ RUNNING / CANCELLED
TODO/PLANNING/RUNNING/FAILED → CANCELLED
FAILED → PLANNING（重试）
非法跳转抛 IllegalTransition → HTTP 409
每次变更写 task_events + event_log 双份留痕
```

## 5. 后端模块详解

### 5.1 Model Gateway（model_gateway.py）

所有 LLM 调用唯一入口，禁止 Runtime 直接调用模型（宪法 Rule 3）：

- `BaseModelProvider.chat(messages, tools, config)` 统一接口
- `OpenAICompatProvider` 已实装；Anthropic/Gemini 为 Skeleton
- `ModelGateway.chat_with_agent_models(model_ids, ...)` 按 primary→secondary→fallback 顺序尝试，单个失败自动切换

### 5.2 Skill System（skill_system.py）

技能即插件：`BaseSkill` 接口（id/name/schema/permissions + execute()）。
内置 text_processing、file_reader、web_search(骨架)。
新技能放 plugins/skills/ 后 `register_skill(Cls)` 注册，GET /skills 自动同步数据库。
Runtime 通过 build_context 把已装技能注入 prompt，不硬编码。

### 5.3 Task Engine（task_engine.py）

状态机校验 + 双日志。`transition()` 是唯一合法的状态变更入口。

### 5.4 Agent Runtime（agent_runtime.py）

单 Agent 执行一个任务的完整周期：

```
创建 AgentExecution → agent.status=RUNNING → 任务 TODO→RUNNING
→ build_context（人格+技能清单+任务内容+模型优先级）
→ 写 planning checkpoint（messages 全量快照）
→ ModelGateway 调用（自动故障切换）
→ 结果写 execution.output + token 用量入 metadata
→ 写 result checkpoint → 成果自动存 Asset(.md)
→ 异常: status=failed + error 留痕
```

暂停 = paused + execution checkpoint；恢复 = 读最新 checkpoint 继续。

### 5.5 Orchestrator（orchestrator.py）——多 Agent 协同核心

```
DECOMPOSE_PROMPT → CEO LLM 输出 JSON {subtasks:[...]}
→ 子任务逐条持久化到 tasks 表（created_by 继承父任务 user_id）
→ asyncio.gather 并行 run_one（每个 worker 独立 session/engine）
→ worker 返回纯 dict {task, agent, status, output}
→ SUMMARIZE_PROMPT → CEO 汇总 Markdown 报告写入 parent.result
→ 失败子任务附于报告尾部（失败隔离）
```

### 5.6 Background Jobs（background_jobs.py）

`submit_async(coro_fn)`：工作线程 + `asyncio.run()` 新事件循环 +
`_make_session_factory()` 新建 asyncpg 引擎。
原因：asyncpg 连接绑定创建它的事件循环，复用主循环连接池必报错。

### 5.7 会话自动回复（routes/messages.py `_agents_reply`）

用户发言 → 后台触发参与者 Agent 依次读取会话历史，
以自身身份生成 ≤100 字回应（最多 3 人，控制成本），写回 messages。

## 6. API 一览

**307 条路径 / 354 个端点 / 52 个分组 / 53 个路由模块**（实测，`app.openapi()["paths"]`）。
除 `auth` 外全部要求 Bearer JWT 认证。交互式文档：http://localhost:8000/docs

> ⚠️ **不要用 `app.routes` 统计路由**：路由是惰性 `_IncludedRouter`，用
> `isinstance(r, APIRoute)` 过滤会得到 0。**`app.openapi()["paths"]` 才是权威清单**，
> `tools/check_api_contract.py` 也是基于它工作的。

下表为 Phase 1 的核心分组（用于理解主干）；当前完整分组按端点数排序如下：

| 分组 | 端点数 | 分组 | 端点数 |
|------|-------|------|-------|
| governance | 23 | collaboration | 9 |
| projects | 16 | skill-growth | 9 |
| agents | 15 | intervention | 8 |
| knowledge-p4 | 14 | automations | 8 |
| war-room | 14 | artifacts | 7 |
| tokens | 13 | computer-agent | 7 |
| organization | 11 | app_alias | 7 |
| tasks | 11 | schedules | 7 |
| meetings | 11 | training | 7 |
| models | 10 | factory | 7 |
| workflows | 10 | external-agents | 7 |
| career | 10 | doc-styles / knowledge / reflection | 6 |
| sandbox | 10 | context / learning / task-dispatch | 5 |
| skills / growth-center | 9 | auth / assets / memory / reviews | 3–4 |

Phase 1 核心分组（历史基线，仍全部可用）：

| 分组 | 端点 |
|------|------|
| auth | register / login / me / logout |
| companies | CRUD + GET /{cid}/departments |
| departments | POST ?cid= / PATCH / DELETE |
| agents | CRUD（列表含 model_configs）+ POST /{aid}/models 绑定 |
| models | CRUD + POST /{mid}/test 连通实测 |
| skills | 列表 / install / uninstall |
| tasks | CRUD + start/pause/resume/cancel + interventions + executions |
| orchestrate | POST 多 Agent 协同（202 异步启动） |
| conversations | CRUD + POST messages（触发 Agent 回复） |
| assets | 列表 / upload(multipart) / 预览 / download |
| events | 按类型/实体筛选的事件流 |
| dashboard/{cid} | Agent统计 + 任务统计 + 活跃任务 + 最近事件/成果 |

详细请求/响应示例见 `docs/03_API_DOCUMENTATION.md`。

## 7. 前端页面

| 路由 | 功能 |
|------|------|
| /login | 登录/注册（未登录访问任何页自动跳转） |
| / | 指挥中心：5 张 Agent 状态卡、任务概览、进行中任务、实时动态（5s 刷新）、「＋下达任务」一键协同 |
| /organization | 公司卡片 + 部门树增删 |
| /agents | 员工卡片墙：状态筛选、创建表单（含部门/主模型绑定）、暂停/恢复 |
| /tasks | 任务列表：状态筛选、行内暂停/恢复/取消 |
| /tasks/[id] | 详情：人工控制面板（暂停/恢复/取消/下达指令/插入消息）、最终报告、员工执行记录、事件流 |
| /collaboration | 会话列表 + 消息流 + 老板发言（触发 Agent 回复） |
| /assets | 资产表格：类型中文显示、来源标识、预览/下载、上传 |
| /logs | 全局事件流水，按 8 类事件筛选 |

界面文案通过 `lib/i18n.ts` 映射表全面中文化（状态/优先级/资产类型/事件类型）。

## 8. 关键实现与踩坑记录

| # | 问题 | 解决方案 |
|---|------|---------|
| 1 | passlib 与 bcrypt 5 不兼容（register 500） | 锁定 `bcrypt<4`（3.2.2） |
| 2 | 后台线程报 "Future attached to a different loop" | asyncpg 连接绑定事件循环 → background_jobs 每任务新建 engine |
| 3 | orchestrator 子任务插入外键失败 | created_by 必须是 users.id，改为继承父任务的创建者 |
| 4 | 汇总阶段读不到 worker 刚写的成果 | 跨 session ORM 缓存 → worker 直接返回纯 dict，禁止跨 session 访问对象属性 |
| 5 | Docker Hub 国内超时 | 镜像前缀 `docker.m.daocloud.io/library/` |
| 6 | alembic autogenerate 报 MissingGreenlet | env.py 用 async_engine_from_config + run_sync（官方异步模板） |
| 7 | Next.js SWC 二进制损坏 | 删除 node_modules/@next/swc-darwin-arm64 重装让 next 自动下载 |

## 9. 开发规则

见根目录 [`AI_EOS_DEVELOPMENT_CONSTITUTION.md`](AI_EOS_DEVELOPMENT_CONSTITUTION.md)，八条宪法要点：
先理解架构再动手｜模块化插件化 API-first｜Provider 禁止写死｜Agent 独立身份｜任务可恢复｜行为全留痕｜支持 Pause/Resume/Cancel｜统一消息协议。

每完成一个功能包需更新 `CHANGELOG.md` 与 `DEVELOPMENT_STATUS.md`；
任务看板在 `docs/development/MASTER_TASK_BOARD.md`（Phase 1 十六项全部 COMPLETED）。

## 10. 测试

### 推荐：一条命令跑完全部检查

```bash
# 在仓库根目录执行；不需要启动服务、不需要模型密钥
./tools/check.sh
```

覆盖：pyflakes（未定义名/语法）→ 应用与 OpenAPI 可生成 → SQLAlchemy mapper 可配置 →
前后端接口契约（404/405 门禁）→ 前端 TypeScript strict → 前端 ESLint → 测试套件。
**任一失败即非零退出**，可直接接进 CI。

### 分别运行

```bash
# 后端测试套件：113 个用例，每个会话独立临时 SQLite，不碰真实库
cd apps/api && .venv/bin/python -m pytest tests/ -q

# 显式排除需要真实模型的用例（CI 里用；无密钥时它们本来也会自动跳过）
cd apps/api && .venv/bin/python -m pytest tests/ -q -m "not requires_llm"

# 前端类型检查与 lint
cd apps/web && npx tsc --noEmit && npx eslint app components lib

# 前后端接口契约（独立运行，便于定位具体错配）
# 覆盖网页端 api.* 调用与移动端 mobile/lib/api.ts 的 call(...)
cd apps/api && .venv/bin/python ../../tools/check_api_contract.py --strict

# 响应形状契约：前端 `api.get<T>` 的 T 与后端顶层 schema 对不对得上
# （路径对了、200 了，但数组被当 {items} 对象读——TS 与 404 都拦不住）
cd apps/api && .venv/bin/python ../../tools/check_response_shape.py --strict
```

### 关于历史脚本

仓库根目录的 `tests/` 下是**历史手工脚本**（29 个），其中 28 个依赖 `/tmp/aieos_token`
临时文件与硬编码的真实数据库 UUID，**无法在 pytest 下运行**。它们已通过
`tests/conftest.py` 从收集范围中排除，保留作为行为参考；
可运行性统计与重写指引见 `tests/LEGACY_SCRIPTS.md`。

当前测试套件位于 **`apps/api/tests/`**：

| 文件 | 覆盖 |
|------|------|
| `test_health.py` | 应用可启动 / OpenAPI 可生成 / mapper 可配置 |
| `test_auth.py` | 弱口令拒绝、登录限流、账号枚举防护、伪造/失效/停用令牌 |
| `test_tenancy.py` | 跨租户读写隔离（含反向对照，避免假阴性） |
| `test_security_sandbox.py` | 12 种注入载荷 / 9 种白名单外二进制 / 8 条越界路径 |
| `test_api_contract.py` | 接口契约门禁（复用 `tools/` 同一份逻辑） |
| `test_code_quality.py` | 未定义名 = 0、`except: pass` = 0、基础设施在位 |

Phase 1 的端到端验收场景（公司→部门→Agent→配置→协同任务→并行执行→汇总→资产入库→
全过程查看）记录详见 `docs/09_TEST_REPORT.md`。
