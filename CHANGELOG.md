# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式。
完整的工程质量改造记录见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)。

## [5.13.0] 单界面工作台：所有功能开在右栏标签里 — 2026-10-30

### ✨ 新首页 `/workspace`

- 三栏：左「数字员工 / 作战会话 / 常用页面」、中「一句话指挥」（附件 / 技能 / 选员工）、
  右「浏览器 / 产物 / 轨迹」。数据全部走现有接口，没有新增后端模型。
- 两侧栏宽度可拖动（7px 分隔线）：指针拖拽 + 方向键微调 + 双击/回车复位；
  左 208–380、右 360–760、中间始终 ≥420；刷新后保留（`localStorage`）。
- `⌘B` 收左栏 / `⌘J` 收右栏 / `⌘N` 新对话（选人）；窄屏 <900px 两侧改为抽屉。
- 右栏：浏览器 = 员工视角正文阅读（`/browser-agent/fetch`）；产物 = 交付文件 + 成果库；
  轨迹 = 该员工近期任务 + `/tasks/{tid}/trace` 时间线（含成本与失败原因）。

### 🚚 路由与桌面壳

- 首页改为 `/workspace`；原指挥中心搬到 `/dashboard`，侧边栏与 ⌘K 均可达。
- 作战会话以标签形式开在右栏（`/warroom/{sid}`）；需要沉浸时点「展开」占满右栏。
- mac 桌面壳改 `titleBarStyle: hiddenInset` + 红绿灯留位；页面顶部让出 34px 并铺拖拽区。

### 🧩 单一界面：所有功能开在右栏标签里

- `/workspace` 成为唯一外壳。侧边栏导航、⌘K、页面内互相跳转的入口全部改为
  `openApp()`——功能页以**标签**形式开在右栏，对话与左栏原地不动，不再整页跳走。
- 右栏标签：「产出」固定第一个，其余按打开顺序追加、可关闭；关掉当前标签落到左边那个（和浏览器一致）。
- 标签内容用 iframe 承载（`?embed=1`，内层隐藏侧边栏/标题栏，Toast/确认框/⌘K 保留）：
  切换标签不丢列表筛选与滚动位置；内层页面自己跳转时标签标题跟着变，但不影响对话。
- 去重按标签**当前地址**：标签跳进详情页后再点左栏同名页面，会新开一个列表标签（不再被带回详情）。
- 焦点在内嵌页面里时，`⌘K`/`⌘B`/`⌘J`/`⌘N` 由内层转给外壳处理（否则会被页面吞掉）。
- 直接访问 `/tasks`、`/knowledge` 等旧地址一律客户端改写到 `/workspace?app=%2Ftasks`，
  地址栏始终只有一个界面；刷新/分享链接都能还原出同一组标签（`?app=` + `localStorage`）。
- 「展开」把功能页吃满中间与右侧（对话留在后台不卸载，Esc / ⌘J / 「回到对话」退出）；
  窄屏上面板本就是整屏，自动隐藏「展开」，顶部只留「回到对话」。
- 原来的全局侧边栏外壳（`components/Layout.tsx`）随之删除，避免两套导航并存。
- `lib/nav.ts` 补登 `/`（跳转别名，`hidden`），导航覆盖用例继续按原断言生效。

### 🌊 聊天流式输出

- 网关新增 `BaseModelProvider.stream()` / `OpenAICompatProvider.stream()` / `ModelGateway.stream_with_agent_models()`：
  OpenAI 兼容 SSE 逐字下发；服务端不支持 `stream_options` 时自动降级；**首个 token 之前**才做重试与换模型，
  吐字之后出错直接上报，避免两个模型的文本拼在一起。
- 新端点 `POST /agents/{id}/chat/stream`（`text/event-stream`）：`status` / `delta` / `done` / `error` 四类帧；
  生成器自带 session，老板消息先落库，中途失败也不会丢掉这条指令。
- 文档指令标记 `[GENERATE_DOC]` 用前缀保留过滤器挡住，流里不会漏出半截系统指令；落库与历史仍是干净正文。
- 前端 `streamPost()`（`lib/api.ts`）复用统一鉴权/401 刷新/超时收口；工作台对话与员工详情对话都改为逐字渲染，
  带闪烁光标、状态行、停止按钮与「本轮未保存到历史」的如实提示。

## [5.12.0] 一人公司主线打通：一句话 → 全公司干活 → 拿到结果 — 2026-10-29

> 目标形态：老板说一句，公司各个岗位自己把活干完，老板直接拿结果。
> 本轮带着这个目标重读代码，发现主链路上有四处**功能上是断的**——
> 派活不启动、任务没工具、Office 产出物打不开、简报时区偏 8 小时——
> 先把它们接上，再补「一句话直达」入口。检查：295 个后端用例 + e2e 38 项全绿。

### 👑 一句话直达（新入口）

- `POST /command/run`：一次请求完成「理解 + 执行」，返回 `plan`（让老板看到 AI 怎么理解的）
  与执行结果。`auto_run=true`（默认）。
- DIRECT 路径：把 `DispatchIn(auto_start=True)` 传下去，任务创建即入队执行。
- PROJECT 路径：作战室 `kickoff` 跳过逐人宣告（成员任务直接标 acknowledged）
  与负责人校对，直接进入 `confirmed` 并派发全员后台执行。
- 指挥台前端：新增「⚡ 一句话直达」按钮，原「下达」改名「先看方案」保留两步式；
  直达同样复用轮询（全终态 / 无存活任务 / 10 分钟硬上限即停）。
- `GET /command/report-templates`：对外暴露报告骨架，前端与 Agent 共用同一份定义。

### 🚀 派活真的会开工

- 新增 `app/task_runner.py`：`start_task`（CAS TODO→RUNNING + 投递）/ `dispatch_auto_queue`
  （按 URGENT/HIGH/NORMAL 拉起，单拍最多 8 个）。
- `Task.auto_start`（可空 Boolean，默认 false）：`ensure_columns` 幂等补列，老库升级无需手工迁移。
- worker 池满（`QUEUE_FULL`）：任务退回 TODO 且**保留** `auto_start`，调度循环下一拍再拉起——
  不再出现「RUNNING 但没有 worker 在跑」的僵尸行。
- 调度循环每拍（10s）独立 try/except 调用自动排队，单次失败不影响其它定时任务。
- `POST /task-dispatch/dispatch` 增加 `auto_start`，返回 `started_count` / `queued_count` /
  每条任务状态；前端「创建后立即开工」默认勾选。

### 🔧 Agent 真正具备取数能力

- 新增 `app/tools.py`：统一工具注册表（`web_search` / `web_fetch`）、唯一执行入口、文本形式
  `<tool_call>` 兜底解析。网络类工具复用 `fetch_page_text` 的 SSRF 闸门与审计日志。
- `run_agent_on_task` 改为工具循环：最多 4 轮、12 次调用，结果截断 6000 字符回填；
  工具失败转结构化 error 让模型换源，不拖垮任务；`metadata_json.tools_used` 留痕。
- `build_context` 注入工具纪律提示（先搜索再作答、标注来源、error 要换源）。
- 能力包工具名对齐真实实现（`web_browsing` → `web_search/web_fetch`；
  `computer_use` → 真实的 `computer_*` 工具名）。
- 任务详情 / 摘要接口返回 `tools_used`，前端执行记录显示「🔧 使用工具」。

### 📊 专业报表与报告

- 新增 `app/report_templates.py`：5 类骨架（行业研究 / 经营分析 / 周报 / 项目复盘 / 方案书），
  按标题+描述关键词命中；自动注入章节结构、数据表规则、「暂缺（建议采集方式）」占位与
  `[GENERATE_DOC]` 产出规范。
- `doc_generator` 重写：
  - Word：真实表格（`Table Grid`）、h1–h4、有序/无序列表、`**加粗**`、
    Normal 字体（中文 eastAsia=宋体）、页边距；
  - Excel：按 `##` 小节拆多 Sheet，数值/百分比还原为数字类型，表头样式、冻结首行、自动筛选；
  - PPT：按 `##` 分页（h1 不再重复封面也不再产生空页）、原生表格；
  - `[GENERATE_DOC]` 标记中的 `\\n` 还原为真实换行。
- 战报/作战室交付物：word/ppt/excel 走真实渲染并登记 `Asset`；LLM 失败时**不落文件**、
  任务标 `failed`（不再把「执行失败」写成交付物）。

### 🕘 简报与收口

- 晨间简报：数据库时间戳是 naive UTC，原实现用本地 `datetime.now()` 比较，
  UTC+8 下"昨日/今日/今日成本"整体偏 8 小时；改为本地自然日 0 点换算 UTC。
- 修掉 `company.created_by` 引用不存在字段导致的 500（非属主访问简报直接崩）。
- 自动化 `call_webhook` 补 SSRF：出站前校验 URL，连接建立后复核真实对端（防 DNS rebinding）。
- 模板建公司（`create_from_template`）补公司归属校验；任务派活目标一律按 `company_id` 过滤。

### 🧪 测试

- 新增 `test_doc_generator.py` / `test_auto_dispatch.py` / `test_agent_tools.py` /
  `test_command_run.py` / `test_briefing.py`，并在 `test_outbound_security.py`
  增补自动化 webhook 的 SSRF 用例；后端 261 → **295**。

## [5.11.0] 按技术方向复审后的第二轮加固 — 2026-10-28

> 来源：按技术方向重做的复审（`docs/审查报告_AI-EOS_2026-10-28.md`，54 项发现 / 10 个 P0）。
> 本轮把其中的 P0 与第二批 P1 全部落地；每一项都跑了对应门禁（261 个后端用例 + e2e 38 项）。
> 升级前请先备份 `data/`（新增的 `event_log.company_id` 由启动时幂等补列）。

### 🚪 安全：越权读清零 + 出站加固

- 15 个「双租户探针确认真的能读到」的端点（专家库、协作消息/协作图、定时任务、
  Agent 记忆、复盘概览、治理自主档位/概览/自改进案例、技能列表、技能成长树、
  应用别名规则、自动化列表、考核周期创建）全部补上归属校验，越权统一 **404**。
- 新增 `require_company_scope()` 路由级门禁（`app/core/permissions.py`）：
  请求带 `company_id` 即校验归属，不带则跳过——挂到 router 上，新增端点默认被覆盖。
  接入 experts / collaboration / memory / reflection / governance / skills /
  skill_growth / automation / review / schedules / command 共 11 个 router；
  app-alias 逐端点加 `assert_company_owned`（`/platform` 不读租户数据，不挂）。
- 静态越权读债务登记表：**52 → 1**（仅剩 `/app-alias/platform` 这一条扫描误报，
  已有行为用例钉住）。护栏同时学会识别 route 级门禁与 `assert_agent_owned` /
  `_assert_selectable_company` 两种既有守卫，不再误报。
- 事件日志补 `company_id` 列与严格读过滤（NULL 不返回）；导出上限 5000 行。
- 模型配置禁止引用服务端敏感环境变量：写入侧（create/update/test/list-models）
  校验之外，`settings.resolved_api_key` 在**读取侧**再兜一层——升级前落库的
  `api_key_env=JWT_SECRET` 历史行，运行时同样取不到值。
- 出站：外部 Agent / 网页抓取 / webhook 关闭自动重定向、逐跳校验对端地址；
  云元数据地址在任何场景（含本地模型）都拒绝。
- Computer Agent：`type_text/click/key/run_shell/run_write/run_app` 纳入高危能力，
  走审批；`execute_nl` 带 `company_id` 并限制在存储根目录内。
- 上传（S9）：1MB 块流式落盘 + `Settings.max_upload_mb`（默认 25）+ 扩展名白名单 +
  服务端生成文件名 + 落盘路径根内校验；超限 413 且不留半成品。
- 技能插件（S8）：删除 `sys.path.insert`，改为唯一模块名加载；manifest 必填
  name/version/module，入口只允许点分模块名且必须位于插件根内；插件根不可写时告警。
  （签名/发布者校验仍未实现，已在代码注释中明确标注。）

### ⚙️ 并发与数据层正确性

- SQLite：WAL + `foreign_keys=ON` + `busy_timeout=15s` + `connect_args timeout=15`，
  每个连接复用 PRAGMA；后台执行器加信号量（`AIEOS_JOB_WORKERS`）与 `QUEUE_FULL` 背压。
- 审批决策、任务/工作流/会议状态迁移改 **CAS**（`UPDATE ... WHERE status=:old` +
  `rowcount`），新增 `runtime_locks` 租约表（TTL），副作用只发生一次。
- 删除路径去掉 `DELETE ... USING` 与 `randomblob()` 等 PG/SQLite 专属写法；
  批量接口修 savepoint 误用；事务拆短，避免长事务持锁。

### ⚡ 性能

- 首页合并为 `/dashboard/{company_id}/bootstrap`：一次返回仪表盘 + 部门概览 +
  运行中任务（`limit 20`，不含 description/result）；计数全部改 `GROUP BY`；
  前端串行 3 请求 → 1 请求，轮询 5s → 15s。
- 新增 `/tasks/{id}/summary` 供轮询：不返回 `result` 全文，execution 输出截断 2KB，
  事件只回最近 100 条；前端首屏与操作后走全量，轮询走 summary。
- 作战室新增 `/war-room/sessions/{sid}/snapshot`：session + team + 聊天增量
  （`since_id`，默认最近 100 条），替代每 3 秒 5 个串行请求；`/agents`、`/companies` 只拉一次。
- RAG：SQL 侧按查询 token 收窄候选集（`limit top_k*20`）后再精排，无命中时回退到
  有界窗口；知识块不再每次全表拉进 Python。技能 scope 批量查询。
- 群聊：显式 `max_tokens=512`、单次 90s、按公司 `Semaphore(4)` 限流、任务描述截断。

### 🖥 Electron / 移动端

- 更新包：URL 白名单 + basename + 强制 sha256 + 解压目录根内校验；`will-navigate` /
  `will-redirect` 拦截；`sandbox: true`；spawn 子进程加 `error` 监听；表单窗口改 `data:` URL。
- 移动端：`/dashboard/{companyId}` 路径修正；审批改为真实端点
  （`GET /intervention/requests` / `POST .../decide`）；Android 增加
  `network_security_config`（targetSdk 34 禁止明文）。

### 🧪 测试与门禁

- 新增：`test_tenancy_read_attacks`（29）、`test_outbound_security`（11）、
  `test_concurrency_guards`、`test_sqlite_concurrency`、`test_delete_portability`、
  `test_events_isolation`、`test_computer_gate`、`test_upload_limits`（8）、
  `test_read_views`（4）、`test_lifespan`（2）。
- 覆盖率门禁进 CI：`--cov=app/api/routes --cov=app/core`，实测 35%，阈值 30。
- `lifespan` 首次被真实执行（此前 ASGITransport 直连，启动副作用从未跑过）。
- 后端用例 **171 → 261**，e2e 38/38 通过。

## [5.10.0] 安全与正确性加固 — 2026-10-27

> 来源：一份五视角独立检测（`docs/检测报告_AI-EOS_2026-10-27.md`）。
> 本轮只做**已复现**的缺陷修复，不加新功能——三个 P0 都是"看起来正常、实际从未生效"的类别。
> 升级前请先备份 `data/`（老库会自动补列，见下文）。

### 🔐 P0-1 JWT 空密钥/公开密钥可伪造 token（最严重）

`jwt.encode(payload, "", "HS256")` 在**空密钥**下照样能签出合法 token，而 `get_current_user`
用同一个空密钥校验通过。`.env.example` 里 `JWT_SECRET=` 是空的，桌面/便携版又不设
`AIEOS_ENV=production`，于是旧的安全闸门形同不存在——实测用空密钥签出的 token
可直接读到他人公司的员工与数据。

- `jwt_secret` 默认值改为空串**并触发密钥引导**：`secrets.token_urlsafe(32)`，
  按 `AIEOS_JWT_SECRET_FILE` → SQLite 同目录 → `storage/` → `apps/api/data/` 顺序落盘（0600、`O_EXCL` 防并发互踢）；
  目录不可写则降级为进程内随机密钥 + 大声 WARNING。**任何路径都不会退回空密钥或公开常量。**
- 生产环境（`AIEOS_ENV=production`）**不自动生成**：仍用公开/空密钥直接拒绝启动。
  自动生成会把"忘记配密钥"这件事悄无声息地糊过去，生产必须炸。
- 桌面壳自己生成并注入密钥（`userData/jwt-secret.key`），不再依赖后端可写。
- 便携启动脚本导出 `AIEOS_JWT_SECRET_FILE` 并提示备份。
- 复验：用 `""` / `aieos-portable-secret` / `change-me` / `secret` / `ai-eos` 伪造 token → **5/5 全部 401**（改造前 `""` 与公开常量均可通过）。

### 🚪 P0-2 跨租户越权（读面为主）

写路径原本有归属校验，**读路径几乎没有**：带着别人的 `company_id` 就能列出对方成果、
知识库文档、审批策略与白名单。

- `deps.py` 新增 `is_company_owned`（批量跳过用，不抛异常）、`assert_kb_doc_owned`。
- `kb2 / artifacts / intervention / tasks / orchestrate` 读写全部补 `assert_company_owned`，
  越权统一 **404 而非 403**（403 会告诉攻击者"这个 ID 存在"，等于租户枚举）。
- 新增护栏测试 `test_every_company_id_endpoint_has_an_ownership_guard`：AST 扫描 GET 端点，
  认「本文件内一跳校验辅助函数」；存量债务登记为 `_READ_DEBT_ALLOWLIST`（69 条，
  **修一处删一处，新增未登记的越权读直接红**）。
- **但静态护栏挡不住"伪校验"**（校验的是调用方自己传进来的 `company_id`，代码里确实"有校验"，
  扫描器看到的是绿灯）。所以再加一份运行时攻击用例 `tests/test_tenancy_attacks.py`（9 例）：
  真实 HTTP 打过去，断 404 且断属主仍可用（防止"用 404 把功能一起锁死"的假修复）。
  并做了反向验证——临时摘掉 `list_artifacts` 的归属校验，用例立刻红（200=泄露），确认后还原。
- 复验：31 项跨租户攻击实测 **31/31 全部拦截**，且正常属主 0 误伤。

### 🗄 P0-3 SQLite 上没有 `NOW()`：发布功能从来没成功过

`POST /artifacts/{id}/publish` 里 `SET published_at = NOW()`——SQLite 无此函数，
必抛 `no such function: NOW`。更根本的是 **`Artifact` 模型压根没有 `published_at` 列**，
所以"发布"这一步在 SQLite（即桌面/便携主形态）上从未生效过。

- 模型补 `published_at` 列 + Alembic 迁移 `a1c7e9c0ffee`；
  启动 `ensure_columns` 对 SQLite/PG 均幂等补列，老库无需手工迁移。
- 写入改绑定参数，统一 **naive UTC**，与 `created_at` / `_now()` 可比。
- 审批策略读取失败改为 **fail-closed**（默认需要审批）——读不到策略不能等于放行交付物出内网。
- 复验：`submit → review approve → publish` 全链路 200，`published_at` 真实落库，重复调用幂等不覆盖。

### 🖥 桌面版默认只监听 127.0.0.1

此前无条件 `--host 0.0.0.0`，同一 WiFi 下任何人都能打到你的后端端口。

- 默认绑定 `127.0.0.1`；新增「局域网共享」开关（开→绑 `0.0.0.0` 并重启后端，关→回到仅本机）。
- `docs / redoc / openapi.json` 只在 `DEBUG=true` 挂载：关 DEBUG 后路由根本不存在（实测 404），
  不再把 312KB 接口清单交给任何扫到端口的人。
- `/metrics/prometheus` 加中间件守卫：配了 `METRICS_TOKEN` 走常量时间比较，未配则**仅回环地址**可抓。
- mDNS 广播与共享开关联动：只监听回环时不再对外宣告一个连不上的服务。

### ⚙️ 后端正确性（本轮一并修的已复现项）

- **会议纪要**：LLM 抽取路径存在死代码/重复取模型，无模型时曾会崩 closing 流程；改为带日志的关键词兜底。
- **预算暂停恢复**：`status == "PAUSED"` 大小写不匹配，超预算后**点提高上限也恢复不了**；改 `func.lower(...)`，兼容历史两种写法。
- **日预算窗口**：`TokenUsage.created_at` 实为 naive UTC，而窗口边界用了 aware datetime → 比较异常导致当日统计失真；统一 naive UTC。
- **工作流**：Agent 节点失败 `rollback` 后隐式返回 `None`，被当成"等待人工"永远挂着；现在标 FAILED 并对称写回错误。
- **阻塞 IO**：`skills.py` 三处 `urlopen` → `httpx.AsyncClient` + 并发抓取（单仓失败只记日志）；`auth.py` 登录限流改 `redis.asyncio`，Redis 不可用降级内存计数。
- **静默异常清零**：`clients / command / experts / performance_engine / mdns_advertise / main / config` 的 `except: pass` 全部补 `logger.warning/error`；`redis_url` 那句"未实际使用"的注释已失效并更正。

### 🎨 前端

- 收口最后一批裸 fetch（`pair`、`UpdateBanner`），下载与轮询走统一层。
- 任务详情页竞态：快速切换任务时旧请求会覆盖新数据 → 加序列号守卫 + 卸载时 `AbortController`。
- `lib/api.ts`：登录入口的 401 不再触发"刷新—重试—再 401"风暴；支持外部 `signal` 取消与 `silent`。
- 错误不再被 `.catch(()=>{})` 吃掉，统一 Toast 播报。

### 🧨 版本字符串六个地方六个值

实测 `pyproject=0.1.0`、`desktop=5.9.0`、`web=5.0.0`、`mobile=4.1.0`、mDNS 广播 `5.4`、
FastAPI docs `0.1.0`——用户报障时截图里的版本号对不上任何一次发布。
新增 `apps/api/app/version.py` 作为后端唯一来源，其余位置从这里取，
并由 `test_version_strings_are_consistent` 跨端钉死（改乱会立刻红）。
五端版本统一到 **5.10.0**（5.3–5.9 仅发布于 GitHub Release，CHANGELOG 未记录，属文档缺口）。

### 🛡 CI 门禁

新增 `.github/workflows/ci.yml`：backend（pytest + compileall，**显式不起 Redis**——
`test_auth` 断言内存回退字典，起了会让限流失效）/ frontend（tsc + vitest）/ contract（OpenAPI、
mapper、`check_api_contract.py --strict`）三个 job，`concurrency` 取消旧任务，无部署步骤。
`tools/check_api_contract.py` 补 `retry`/`takeover` 动作，并把"路径中段变量"从宽松匹配
改成还原真实字面量后**逐条要求后端路由存在**（拼错动作名不再报绿）。

### ✅ 验证

| 项目 | 改造前 | 改造后 |
|---|---|---|
| `uv run pytest` | 159 例，**3 失败** | **171 passed, 1 skipped, 0 失败** |
| 跨租户攻击实测 | 读路径普遍可越权 | **31/31 拦截**，属主 0 误伤 |
| 伪造 token（公开/空密钥） | 可通过 | **5/5 拒绝（401）** |
| `npx tsc --noEmit` | — | 0 错误 |
| `npm run test`（vitest） | — | 39/39 通过 |
| 契约门禁 `--strict` | 3 处红 | 通过 |
| `./tools/check.sh`（含端到端主链路真进程试跑） | — | **全部通过，e2e 38/38** |

### ⚠️ 已知限制与待决策

- **`alembic upgrade head` 只支持 PostgreSQL**：链路里 `23e9b97bb927` 在 SQLite 上会抛
  `No support for ALTER of constraints`（历史遗留，非本轮引入）。桌面/便携形态走
  `create_all + ensure_columns`，不受影响；`服务器一键部署.sh` 已容忍该失败。
  但 `docs/08_DEPLOYMENT_GUIDE.md` 让 SQLite 用户直接跑 alembic，属文档错误。
- **Mac 代码签名私钥已提交进仓库**（`packaging/mac-signing/*.p12(.b64)`，口令 `aieos2026`，
  且脚本会把它装进系统信任根）。这是不可逆操作（改写 git 历史 / 轮换证书），
  **需要 maintainer 决策**，本轮未改动。
- 注册接口仍匿名开放；绑定收口后风险面已从"整个局域网"缩到"本机 + 显式开启共享"。
- 读端点归属债务 69 条为**基线登记**，非"已修完"。

## [5.2.0] 客户端自动升级 — 2026-10-18

> 各端客户端启动后自动从「你自己的服务器」检查并升级，不再需要手动找 Release 重新下载。

### 后端：更新通道 API（apps/api/app/api/routes/clients.py）
- `GET /clients/update/check?platform=&arch=&current=`：与 `storage/clients/manifest.json` 比对版本号，返回是否有新版 + 安装包下载 URL + 大小 + 更新说明；manifest 未部署时优雅返回 `has_update=false`（客户端静默，零影响）
- `GET /clients/update/download/{filename}`：下发 `storage/clients/` 下的安装包（防目录穿越）

### 桌面壳（Electron）
- 启动后自动检查更新：新版本 → 底部浮出升级横幅（新特性说明 + 进度条）
- 一键升级（按平台自动应用）：
  - **Windows**：NSIS 静默安装（`/S`）→ 自动重启，全程无感
  - **macOS**：自动打开 dmg 安装器并引导替换 /Applications（替换系统目录需要管理员权限，无法完全静默）
  - **Linux**：AppImage 场景原地替换并自动重启；权限不足时引导手动替换
- 菜单「查看 → 检查更新」手动触发；下载/安装/重启进度事件实时回推前端

### Android（Expo）
- 服务连接页新增「自动升级」区：检查更新 → 发现新版 → 下载 APK（带进度）→ 系统分享面板选择「安装」（react-native-share）

### 发布工具（tools/publish_client_updates.py）
- 一条命令从 GitHub Release 拉 5 个主安装包 → 写入服务器更新目录 + 生成 manifest.json
- 凭证自动读 `~/Documents/张阗/.github_token`（或 `GH_TOKEN`）
- 用法：`python3 tools/publish_client_updates.py [--release 标签] [--dir 目录]`

### 验证
- 后端 check/download API 全链路通（真实 manifest + 真实安装包）
- web / mobile / desktop 三方类型与语法检查全过

## [5.1.0] 多平台客户端（Mac / Windows / Linux / Android）— 2026-10-18

> 把 AI-EOS 打成「直接安装」的客户端安装包。iOS / 鸿蒙本迭代不做。

### 桌面客户端（Electron，desktop/）
- 新品牌图标（深色玻璃拟态 + 渐变轨道环 + AI/EOS 字标 + 呼吸点），多尺寸 ico/png
- 主进程重构：服务器地址可配置（`userData/settings.json`，可指向本机便携 / 远程自托管）
  - **打包安装场景**：app 内无仓库文件时不再强拉服务——探测不到服务显示「服务未检测到」引导页（玻璃拟态，可重试 / 换服务器 / 打开数据目录）
  - **开发场景**（仓库内 `npm start`）保留原行为：复用已监听端口，否则自拉起后端(8001)+前端(8000)
- 产物：`AI-EOS-5.1.0.dmg`（Intel）/ `AI-EOS-5.1.0-arm64.dmg`（Apple Silicon）/ 同名 zip ×2
- 验证：electron-builder 双架构构建 0 错、SMOKE 冒烟 0 错、打包 .app 实跑正常加载运行中的服务

### Android 客户端（Expo，mobile/）
- 老板视角 App（KPI / 一句话 / 签字门 / 连接服务器），V4.1 起已有
- 本次：`versionCode` 补齐、真图标替换 1×1 占位、版本 → 5.1.0
- 自签 p12 keystore（`packaging/keystore/ai-eos.p12`，100 年有效期）→ gradle `assembleRelease` 出发布版 APK

### 客户端流水线（.github/workflows/clients.yml）
- 4 平台并行：macos-latest(dmg+zip) / windows-latest(NSIS 安装器 + 绿色版) / ubuntu-latest(AppImage + deb + Android APK)
- 汇总 job 自动发 GitHub Release，挂全部安装包（tag `clients-*` 或手动 dispatch 触发）
- 说明：安装包均为内部自签（未做 Apple/Windows 公证）——分发给自己的设备时按系统提示放行即可

## [5.0.3] 附件口补全（建团队 / 冷启动 / 部门 / 项目）— 2026-10-18

> 延续 V5.0.2 全系统附件口：把剩余输入表单的📎接通，并修掉一处「附件口在 UI 但 payload 漏发」的假成功。

### 附件口补全
- **建团队（TeamGenModal）/ 冷启动向导（OnboardingWizard）**：建公司入口接📎挂资料，`team_generator.GenerateIn` 收 `attachments` 并落库到新公司
- **组织架构页（部门）**：新建/编辑接📎；`DepartmentPatchIn` 加 `attachments`，编辑时载入已有附件、PATCH 带回，避免把附件覆盖成空
- **项目页**：建项目表单早已有📎口但创建 payload 漏发 `attachments`（用户挂了文件却不入库，属假成功）——`projects` 表 + ORM + `ProjectIn` + 前端 payload 全链路补上

### 验证
- 前端 `tsc` 0 错、`next build` 0 错
- 后端：projects 带附件创建 + 读回 200；team-generator preview 收 attachments 200；测试数据已清理

## [5.0.2] 全系统附件口 + 级联删除修复 — 2026-10-17

> 输入表单全系统接通📎附件口（创建时真实落库、列表回传）；修删公司 500、外部接入注册 422 两个 P0。

### 全系统输入表单接通 📎 附件口
- 数据层：`companies / departments / automation_definitions / automation_schedules / training_sessions / external_agent_connections / agent_factory_jobs` 7 张表统一加 `attachments JSON` 列
- ORM：7 个模型类 + 7 个路由入参模型（`CompanyIn`/`DepartmentIn`/`DefinitionIn`/`ScheduleIn`/`SessionIn`/`ConnectionIn`/`BlueprintIn`/`AgentIn`）接收 `attachments`，创建时落库、列表端点回传
- `Agent` 无附件列 → 走已有 `memory_config` JSON 列存引用（零 DDL）
- 前端 6 页接入通用 `AttachmentPicker`（真实上传）：数字员工 / 自动化规则 / 调度 / 训练 / 外部接入 / 蓝图画布

### 修 P0：删公司 500（级联缺口）
- 根因：指向 `companies.id` 的 9 张子表外键全为 `NO ACTION`，删公司时子行残留 → `IntegrityError`；且 `Company.departments` 缺级联，ORM 把子行 `company_id` 置 NULL 撞 NOT NULL
- 数据库层 10 张子表外键改 `ON DELETE CASCADE` + ORM `ForeignKey(ondelete="CASCADE")` + `Company.departments` 加 `cascade="all, delete-orphan"`
- 实测：删带部门的公司 `500 → 204`，子行父行全清

### 修契约错位：外部接入注册 422
- 前端发 `provider`，后端 `ConnectionIn` 必填 `provider_type`（claude/codex/hermes 实际为 API/LOCAL/REMOTE 枚举）→ 字段名对齐，注册 `422 → 201`

### 验证
- 前端 `tsc --noEmit` 0 错误；后端全文件 import OK
- 7 类资源「带附件创建 → 读回 attachments 落库 → 级联删除」全链路回归通过，测试数据已清理

## [5.0.0] 专家 × 组织形态版 — 2026-10-17

> 大版本：新增数据模型（agent 绑定专家）+ 跨模块能力（专家库管理 / 数字员工联动 / 行业组织形态一键落地）。

### 专家库补全到 302（缺口 40）
- 起初 vendor 导入漏抓上游子目录，缺 40 个专家（游戏系 Unity/Unreal/Godot/Roblox/Blender、工程深水区 RAG/隐私/WebAssembly/支付/实时协同、安全审计、测试自动化等），非表面的「10 个」
- `tools/rewrite_experts.py` 后台补齐 40 位，全部过 10-gram 相似度门（最高 0.0150 < 0.02），概念简报仅喂「职业名+部门+emoji」
- 专家库 262 → **302**，`import-original` 全量入库，前端可见 302 位

### 专家库三大操作能力（编辑 / 复制 / 手动新建）
- `PUT /experts/{id}` 编辑专家：改 姓名/emoji/部门/简介/system_prompt，重提交付物章节
- `POST /experts/{id}/copy` 复制专家：生成「名称（副本）」，继承人设章节
- `POST /experts/manual` 手动新建专家：不依赖 302 预设，凭空定义人设（`source_slug=custom-*` 与预设隔离），**表单必带 📎 附件口**
- 前端：详情弹窗加「✏️ 编辑」「📋 复制」；页头「＋ 手动添加专家」弹窗（玻璃拟态 + 附件选择器）

### 数字员工 × 专家库打通
- `agents` 表新增 `expert_template_id` 列（关联 expert_templates.id）
- 「招新员工」：📚 从专家库选择（可搜索 302 位）→ 自动回填姓名/岗位/部门/人设；也可纯手动，两者共存
- 「批量添加」双模式：✍️ 手动输入 / 📚 专家库勾选（多选一键批量生成，带 expert_template_id）
- 后端 `create_agent` / `batch_create` 绑定专家时自动取 system_prompt 作人设 + 雇佣计数

### 行业组织形态预设 + 一键引入
- `content/org_presets/*.json`：内置 8 行业（互联网/制造/零售电商/医疗/教育/金融/建筑地产/政府），共 56 岗位，每岗位推荐对应专家
- 新增 `org_presets.py` 路由：`GET /org-presets` 列表 · `GET /org-presets/{id}` 预览 · `POST /org-presets/{id}/apply` 一键落地（部门树 + 配备专家的数字员工）
- 支持 dry_run 预览、按部门勾选引入、幂等（已存在自动跳过）、统一模型绑定
- 前端组织架构页：🏢 组织形态 选择器 → 预览弹窗（部门可勾选 + 已存在数提示）→ 一键引入

### 文案清理（展示层去原创字样，合规底裤保留）
- 删专家库「📜 原创声明横幅」+「AI-EOS 原创版」「AI-EOS 原创」展示文案（登录页/副标题同步改「专家 × 组织形态版」）
- **保留** `docs/EXPERTS_STATEMENT.md`（法务/审计看）· `vendor/.../LICENSE`（MIT 合规）· DB 的 source/license 字段（数据层留痕，不展示）

### 版本标识
- package.json / 登录页 / 侧边栏 → V5.0 · 专家 × 组织形态版

## [4.2.0] 专家全原创版 — 2026-10-17

### 桌面客户端（V4.0-P1）
- 新增 `desktop/`：Electron 壳（main.cjs + preload.cjs）把便携后端(FastAPI/SQLite@8001) + Web 前端(Next.js@8000) 装进原生窗口，不重写任何 UI
- 主进程平台感知：dev 场景复用已监听端口，双击便携场景自拉起子进程（uvicorn + next start）并等待就绪
- 数据单文件 `data/aieos.db`；API 端口烘焙一致性检查（.next 不对就自动重建）
- electron-builder 三平台打包配置（mac dmg/zip、win nsis/portable、linux AppImage/deb）
- 修复：Electron 关闭路径递归死锁（will-quit preventDefault 死循环）；冒烟测试模式 `AI_EOS_DESKTOP_SMOKE=1`

### 移动端（V4.1 · 老板视角）
- 新增 `mobile/`：Expo Router + React Native 老板随身 App（KPI / 一句话下达 / 签字门 / 服务连接 四 Tab）
- 一句话下达含📎附件口，方案必须老板"确认执行"才真跑（延续可视化确认点哲学）
- 共用与桌面壳/后端同一套 API（`/auth` `/command` `/experts` `/dashboard`）；token 走 expo-secure-store 加密持久化
- Metro 打包已验证：900 modules → 2.5MB HBC，TypeScript 类型检查归零
- 签字门真审批 / 呼吸灯动画 / 附件选图 留 V4.2

## [4.2.0] 专家全原创版 — 2026-10-17

### 专家库内容 100% 原创重写（商业化合规）
- 全部 262 位专家人设由 AI-EOS 团队 LLM 独立完整重写，存 `content/experts/`（source=ai-eos-original，license=AI-EOS 原创）
- 合规红线：生成时只喂「职业名+部门+emoji」概念层，**绝不喂上游正文**；生成后 10-gram 相似度门对上游原文硬拦截（全量最高 0.0199 < 0.02）
- 新增 `POST /experts/import-original`：按 source_slug 原地替换旧 MIT 记录 + 自动下架未覆盖的上游记录
- 三重留痕：`docs/EXPERTS_STATEMENT.md`（完整声明）· 专家库页面原创声明横幅 · 每条记录 source/license 字段
- vendor 瘦身：上游 266 份原文移入 `vendor/agency-agents-zh-archive/`（本地留档，.gitignore 不进仓库），主目录仅留 LICENSE 等 5 个合规文件
- 批量生成器 `tools/rewrite_experts.py`（并发 + 429 退避 + 断点续跑 + 相似度门）

### 专家库「完整展示」升级（参考上游一页一份档案形式）
- 详情弹窗从「截断简介 + raw_md 堆叠」升级为**章节化人设档案**：8 大章节（身份记忆/核心使命/关键规则/交付物/工作流程/沟通风格/成功指标/进阶能力）结构化卡片，前 2 节默认展开
- 章节标题按类型着色（使命=青 / 规则=琥珀 / 交付物=翠绿 / 指标=绿 …），保留 Portal 居中弹窗 + 玻璃拟态设计体系
- 卡片「AI-EOS 原创」绿色标识，替代旧的「MIT 许可」字样

### 版本标识
- 登录页 / 侧边栏 / package.json → V4.2 · 专家全原创版

## [4.0.0] 一句话指挥台 — 2026-10-17

### 一句话指挥台（Command Deck）
- 新增 `POST /command/plan`：一句话 → LLM 判断走 DIRECT（直接派活）/ EXPERT（专家库组队）/ PROJECT（作战室立项），方案不落库
- 新增 `POST /command/execute`：老板确认后执行，三路全部复用既有引擎（task_dispatch / experts / war_room）
- 新增 `GET /command/report/{ref}`：执行摘要 + 单文件 HTML 报告（可直接发群分享，含 token 费用）
- 新增 `POST /command/notify`：报告推钉钉/飞书/企微 webhook（按域名自适应消息体）
- 前端「📣 一句话指挥台」页（指挥中枢分组）：一句话输入 + 📎附件口 + 方案确认弹窗（含预计耗时/分工/理由，可改可拒）+ 执行结果控制台 + 报告分享 + 群推送
- 修复：Postgres 模式 tasks.progress / current_stage、agent_executions.completed_at 缺列（补 tools/ensure_columns.py 一键补列脚本）
- 登录页/侧边栏版本标识 → V4.0 · 一句话指挥版

## [3.0.0] 专家融合版 — 2026-10-17

### 专家库融合（agency-agents-zh · MIT）
- 新增 `expert_templates` 表 + 导入器（vendor/agency-agents-zh，265 位专家）
- 专家 API：`/experts/import`（幂等导入）、列表/详情/部门统计、
  `POST /experts/search`（P1 自然语言找专家）、`POST /experts/team`（P2 一句话组队）
- 专家 → 工厂蓝图：`POST /experts/{id}/to-blueprint`，走既有 审查→验收→上岗 确认链
- 前端「📚 专家库」页（组织与智能体分组）：部门筛选、卡片、详情弹窗、雇佣、找专家、组队
- 登录页/侧边栏版本标识 → V3.0 · 专家融合版

## [2026-10-14] — 五视角代码审查整改升级

> 依据五视角审查报告的 Top 10 + P0/P1/P2 整改项。升级前已全量备份
> （`~/Documents/张阗/ai-eos-backup-20261014`）。Mac 无 shell，所有改动为静态编辑，
> Python 文件已过 `py_compile` 语法校验；**上线前请在 Mac 上跑一遍 `pytest` 回归**。

### 任务执行链路（重度用户 P0）

- 后台引擎泄漏修复：`_with_fresh_engine` 的 finally 里 `await engine.dispose()`
- 超时按任务类型拆分：会议 600s，任务/编排/工作流读 `AIEOS_TASK_TIMEOUT_S`（默认 7200s）；
  超时先写事件告知用户再标 FAILED
- 新增 `POST /tasks/{tid}/retry`：FAILED→PLANNING→RUNNING 并重新提交后台工作；
  前端任务详情 FAILED 状态加「🔁 重试」按钮
- `resume_execution` 改为真重跑（从 checkpoint 恢复，旧执行标 `superseded`）；
  暂停按钮旁提示「暂停将在当前步骤完成后生效」
- lifespan 新增 `recover_stuck_tasks()`：RUNNING/PAUSED 超 30 分钟（`AIEOS_STUCK_TASK_MINUTES` 可配）
  无心跳标 FAILED 并写事件
- Task 模型新增 `progress`（Float，默认 0.0）与 `current_stage`（String，可空）；
  前端任务列表/详情加进度条
- `schedules.py` pause/resume 补归属校验；`departments_batch.py` cascade 改为真递归删除（已核查外键安全）
- `automation_engine.py` 新增 `run_workflow` / `call_webhook` / `update_task` 三个真实动作
- 编排链路补「产出即归档」：CEO 汇总后调 `save_artifact`（此前只写 `parent.result`）

### 模型网关与计量（程序员/运营 P0）

- `record_token_usage` 只 add 不 commit，提交权交还调用方（已审计全部调用方外层均会 commit）
- `usage_ctx` 改为上下文管理器（退出 finally reset）；training / factory / meeting / chat 补齐 agent / company 维度
- failover 每次失败记 `logger.warning`，最终异常附全部失败摘要；失败调用记入 token 流水（`success=False`），
  `model_calls.success_rate` 口径修正
- 简易熔断/冷却：单模型连续失败 5 次冷却 5 分钟，429 直接短路；猴子补丁转正为类方法
- `PROVIDERS` 支持 `plugins/providers/*.py` 动态加载（`register()` 约定，单文件失败隔离）
- `knowledge_engine.retrieve` 加 `limit` 分页（默认 200，SQL 层截断）

### 架构与安全（程序员 P0/P1）

- `plugins/skills/` 真动态加载（`manifest.json` + `importlib`，单 manifest 失败只记 warning）
- `test_code_quality.py` 静默吞异常检测升级为 AST 级（行尾注释 `pass` 不再逃逸；
  `agent_runtime.py:64-65` 已同步修复为 `logger.warning(..., exc_info=True)`）
- `app/sandbox.py` 改名 `app/computer_workspace.py` 消歧（import 已全仓同步）
- 新建 `app/engines/` 包：新引擎公约 + 渐进迁移文档（现有引擎暂不搬移）
- RefreshToken：`POST /auth/refresh`（轮换，响应 `{access_token, refresh_token}`）+ `POST /auth/logout`（吊销），
  库里只存 SHA-256 哈希；附 Alembic 迁移
- 登录限流 Redis 优先（无 Redis 回退内存 dict，对外行为不变）
- `ensure_columns` 只在 sqlite 方言下运行（PostgreSQL 交给 Alembic）

### 前端与向导（小白 P0/P1）

- 全局快捷键：`n` 新建任务、`/` 聚焦搜索、`j/k` 列表导航、空格多选、`?` 帮助面板
  （`lib/shortcuts.ts` + `GlobalShortcuts.tsx`）
- 术语统一：「＋ 招新员工」、「人设与职责设定」、「OpenAI 兼容地址」、「首选模型」；⌘K 占位与 keywords 对齐
- 向导步骤 2：每个模型预设加「Key 从哪来」一句话指引；`.env` 指引重写为小白 4 步操作（含便携包无 `.env` 情况）
- 建员工自动绑定公司首选模型（`governance.create_from_template`）；新增免 Key 纯前端演示模式（零 DB 写入、全程标注「演示」）
- `lib/api.ts`：401 自动调 `/auth/refresh` 换 token 重试一次；登录页存 `refresh_token`
- vitest：`vitest.config.ts` + `lib/api.test.ts` + `lib/validate.test.ts`（静态正确，未运行）

### 交付运维（运营 P0/P1）

- **LICENSE**：BSL 1.1（BUSL-1.1）——Licensor eos.hyipf.com（2026-10-14 晚由
  山东阗言密语寅卯辰文化传播有限公司变更）；免费层：个人学习/研究、非商业用途、
  团队规模不超过 3 人的团队内部使用；超 3 人使用或云端/公网/SaaS 部署须商业授权
  （登记 eos@hyipf.com）；Change Date 2030-10-14（满 4 年转 Apache-2.0）；
  对外称 source-available；`pyproject.toml` / `package.json` 同步标识
- 打包说明：账号三处收敛为「首次使用请在登录页注册」（删弱口令节）；`rm -f data/aieos.db` 改为先备份再重建；
  Python 要求更正为 3.12+
- `start-mac.sh`：`cd` 到 `apps/api`、自动装依赖、日志 `>>` 追加、启动前快照（留 5 份）；
  `start-win.bat` 纯 batch 重写（补 `sqlite+aiosqlite:///` 前缀，修 npm 未加 call 的 bug）
- 根目录 `.env.example`（中文注释模板）；`/health/db` 异常返 HTTP 503；日志按日轮转（`AIEOS_LOG_DIR` 可配，保留 30 天）
- `GET /metrics/prometheus`（手写 exposition，无新依赖）；`GET /events/export`（csv/json 流式）；
  成果删除 + zip 打包下载；资产改相对路径（兼容旧绝对路径）；kb 上传与截屏移出 /tmp
- `docker-compose.yml` 生产化：api/web 服务与 Dockerfile、`restart: unless-stopped`、healthcheck、
  PG 密码走 `docker/.env`、redis AOF 持久化卷、端口只绑 127.0.0.1
- 新建 `docs/BACKUP_RUNBOOK.md`、`docs/UPGRADE_SOP.md`、`docs/DATA_FLOW.md`（默认网关 `https://api.agnes-ai.cn/v1` 数据流向告知）

### 已知局限（本次未覆盖）

- Mac 无 shell：未运行任何测试；vitest / tsc / next build 均未验证。**跑 `pytest` 时不要起本地 Redis**，
  否则 `test_auth.py` 对 `_login_failures` 内存 dict 的断言会失败（限流走 Redis 通道时）
- 429 熔断语义实现为「短路 break 不再试后续模型」（同 provider 限流假设）；如需「跳过中间只试最后一个 fallback」可再调
- 演示模式为纯前端模拟（未落库、未调模型）；如需真实演示任务落库需后端配合新增演示执行路径
- `resume_execution` 由「状态翻转」改为「真重跑」，属预期内的语义变更；若旧测试断言旧行为需同步更新
- `start-mac.sh` 仍绑 `0.0.0.0`（有局域网访问需求，未改）；`start-linux.sh` 未动

## [未发布]

### 已新增（端到端试跑 · 无需真实密钥）

- **端到端主链路试跑** `tools/e2e_smoke.py`：起真实 uvicorn 进程 + 临时 SQLite +
  本地假 OpenAI 兼容模型服务，跑通「注册 → 登录 → 建公司 → 注册模型 → 建员工 → 绑模型 →
  建项目 → 建任务 → 启动 → 轮询到完成 → 断言成果入库 / 文档落盘 / token 记账 / 模型确实被调用」，
  **38 项断言全部通过**，已并入 `tools/check.sh` 作为第 7 道关卡
- 因为 `.env` 的模型密钥为空，起一个本地假模型即可让真实进程跑完整条链路 ——
  **整条主链路的验证不需要任何真实密钥**
- **试跑抓出并修复 4 个真实缺陷**：
  - 模型自检只看 provider 默认环境变量、不看模型自己的 `api_key_env` → 可用模型被误报「缺密钥」
  - `POST /projects` 只认 query 参数，前端传 body → 422，项目创建不了
  - 任务失败的证据写在未提交事务里，被上层 rollback 冲掉 → 失败现场查不到
  - `AgentExecution.completed_at` 运行时在写，但表上没有这一列 → 执行耗时永远是空

### 已修复（权限层 · 12 处跨租户越权）

- **修掉 12 处跨租户越权（IDOR）**：10 个完全没有归属校验的 DELETE 端点 +
  2 处「伪校验」（拿记录里的 `company_id` 与**调用方自己传进来的** `company_id` 比对 ——
  攻击者把自己公司 ID 传进去就永远通过，比完全不校验更危险，因为它会让 review 的人放心）
- **新增权限层** `app/core/permissions.py`：Capability（查看 / 操作 / 管理 / 破坏性 / 管理公司）
  × Role 矩阵；越权返回 **404 而非 403**（403 会暴露「此 ID 存在但你没权限」，等于帮攻击者枚举租户）
- **静态扫描护栏**：新增无归属校验的 DELETE 端点会直接让测试变红（已反向验证）
- **诚实边界**：目前只有 OWNER 角色真正可达；ADMIN / MEMBER / VIEWER 是已声明但尚未接入的语义，
  需先建「公司成员」表再接入，本轮未假装完成

### 已新增（度量体系）

- **`GET /metrics/overview`**：任务 / 执行 / 模型调用三级成功率、平均耗时与延迟、
  失败原因归一化（脱敏 UUID）、按员工归因、按天吞吐；前端「绩效中心」已接入
- **关键设计：无样本返回 `null` 而非 `0`**。「还没跑过任务」显示「—」，
  「跑了 2 个失败 1 个」显示 50% —— 二者绝不能混为一谈，测试里专门断言了这一点
- 修掉 `GET /dashboard/{cid}` 用 **200 + `{error}`** 表示失败（HTTP 层完全看不出出错了）
- **数据库 schema 自动补列** `app/db/migrations.py`：`create_all` 只建表不加列，
  升级后新字段会静默缺失，现在启动时自动补齐（跳过非空列并告警，绝不删列改列）

### 已修复（工程质量改造 · 详见 UPGRADE_NOTES.md）

- **修复 9 处未定义变量**导致的静默失效。其中「文档风格 + 公文写作」整块能力此前
  100% 不执行，但接口仍返回 200；三个接口（文档风格删除、技能删除、训练撤销）必然 500
- **修复 11 处前后端接口契约错配**。最严重的是人工介入页：路径错（404）+ 缺鉴权头（401）
  + 无视响应无条件提示「已批准」，用户以为成功而实际什么都没发生
- **治理 28 处静默吞异常**（`except: pass`）。保留「失败不阻塞主流程」的原意，
  但每处失败都留下可检索、可归因的日志
- **修复密钥加载链路**：此前全项目 0 处 `load_dotenv()`，只在 `.env` 配密钥会取到空串，
  导致调用模型报 401
- **修复任意文件读取**：`FileReaderSkill` 无路径限制，可读取 `.env` 并取走 `JWT_SECRET`
- **修复命令注入**：`real_run_write` 未过滤 `;` `|` `>` 与换行却用 shell 语义执行
- **补齐 4 个模块的跨租户归属校验（IDOR）**，含「可替他人公司授权本机 shell 能力」
- **新增生产环境安全门禁**：`JWT_SECRET` 仍为占位值或 `DEBUG=true` 时拒绝启动

### 已修复（消费者体验收敛）

- **统一二次确认，替代原生 `confirm`**：浏览器可被设置为"不再显示此类对话框"，
  之后 `confirm()` 恒返回 `true` —— 二次确认会退化成一次点击就执行破坏性操作
- **为 3 处原本完全没有确认的破坏性操作补上护栏**：批量删除公司（连同项目/员工/成果/日志）、
  执行离职交接（真实改写归属且不可撤销）、删除经验教训与记忆
- **14 处原生 `alert` 改为非阻塞 Toast**；其中会议页的上传失败原先被空 `catch {}`
  静默吞掉，用户会误以为"传上去了但没生效"
- **修掉两处"点了按钮没反应"**：新建 Agent 时姓名为空直接 return、模型配置格式错误
  只返回技术性 422

### 已新增（消费者体验收敛）

- **`⌘K` 全局跳转**：35 个页面按标题、说明、英文名、拼音、口语说法全量可搜，
  支持「最近访问」与全程键盘操作；内部页面改为"藏起来但搜得到"
- **导航单一事实源 `lib/nav.ts`**：侧边栏与命令面板共用一份定义，新增页面只改一处
- **统一确认框 `components/ConfirmDialog.tsx`**：主题化、逐条列出影响面、
  不可逆操作要求逐字输入确认词、Esc/Enter/Tab 全键盘可用、关闭后焦点归位
- **表单校验体系** `lib/validate.ts` + `lib/useFormValidation.ts` + `components/Field.tsx`：
  默认不打断用户、提交时一次性列出全部问题、字段级 `aria` 关联
- **6 条体验护栏测试**：禁止原生弹窗、每个页面必须有入口、导航不得有死链、
  导航必须带说明与检索词（已反向验证：注入违规探针会让门禁变红）
- **测试体系重建**：113 → **158** 个离线用例（原 29 个「测试」文件里 28 个无法运行）
  - 覆盖认证与限流、账号枚举防护、跨租户隔离、沙箱与命令注入、接口契约、
    schema 迁移、度量、权限与代码质量与体验护栏

### 已新增（前端数据层与类型统一）

- **30+ 处裸 `fetch` 全部收口到 `lib/api.ts`**：此前各页面自己拼
  `NEXT_PUBLIC_API_URL` 与 `aieos_token`，导致没有超时（后端卡住页面永久转圈）、
  没有错误规范化（422 显示 `[object Object]`），且**绕过统一层 = 同时绕过契约校验**
- **收口后挖出 6 个此前从未生效的功能**：
  - 员工换模型：前端发 PATCH，后端是 `PUT /agents/{id}/switch-model` → 从来没生效过
  - 外部接入启用/停用：前端 `PUT /external-agents/{id}/ENABLED`，
    后端只有 `POST .../enable|disable`，且原来连鉴权头都没带
  - 4 处下载链接点了必然 401 —— 下载接口要 Bearer token，而 `<a href>` 不会带它；
    改为取回 blob 再本地保存（新增 `fetchBlobURL` / `downloadURL` / `downloadText`）
  - 日志导出 `window.open(接口地址)` 打开的是 401 的 JSON
  - 多处 `createObjectURL` 后立刻 revoke，下载有概率失败
  - 契约扫描被模板里的三元表达式骗过 4 次（引号提前终止捕获）→ 已加「无法静态解析」判定
- **`lib/types.ts`** 领域类型 + **`lib/useResource.ts`** 轻量数据层
  （并发去重 / 失效重取 / 乐观更新，约 100 行，**不引第三方依赖**）
- 全站 `any` 从 136 处降到仅剩 `catch` 子句（TS 无法给 catch 标注类型）与 LLM 自由结构字段
- **新增护栏**：禁止裸 fetch / 硬编码地址 / 手工取 token（已反向验证：植入探针立刻变红）

### 已新增（工程质量改造）

- **交付前自检门禁** `tools/check.sh`：一条命令跑完静态检查 / 导入 / mapper /
  接口契约 / 类型检查 / lint / 测试，任一失败即非零退出
- **接口契约校验工具** `tools/check_api_contract.py`（已接入门禁）
- **文件访问沙箱** `app/core/sandbox.py`：默认拒绝 + 路径解析 + 敏感文件拦截
- **集中日志** `app/core/logging_config.py` + 全局异常处理器
- **环境加载器** `app/core/env_loader.py`
- **本机能力服务层** `app/services/computer_service.py`：权限校验 / 执行 / 审计收敛为唯一实现
- **可复用归属校验** `assert_company_owned` / `assert_agent_owned`
- **前端全局反馈**：Toast、三态布局、`error.tsx` / `loading.tsx` / `not-found.tsx` /
  `global-error.tsx`、`prefers-reduced-motion` 支持
- **新用户三步引导向导** + 模型可用性自检接口（只报告密钥是否存在，不返回密钥本身）
- **前端 API 层重构**：180s 超时、可区分的 `ApiError`、校验错误规范化、泛型响应
- **版本控制**：此前无 `.git`，现已有基线快照与按问题切分的提交历史

### 已变更（文档校订）

- README / DEVELOPMENT / INTRODUCTION 的数字按实测校正：
  Phase 1 → **Phase 3**，38 → **354 端点**，18 → **94 张表**，约 2400 行 → **21,554 行**
- 删除 README / INTRODUCTION 中「测试账号 `boss@aieos.dev` / `Passw0rd!`」的说明 ——
  该账号在整个代码库中**从未被创建**，属历史遗留；现改为引导用户自行注册
- 修正 `CHANGELOG.md` 与 `DEVELOPMENT_STATUS.md` 中的字面 `\n` 转义符（此前渲染为单行）

---

## [0.1.0] — 2026-08-23

### 新增（Phase 1 MVP 全量交付）

组织架构 / Agent 管理 / 模型网关 / 技能插件 / 任务引擎 / Agent Runtime /
多 Agent 协同 / 协作消息 / 资产中心 / 事件日志 / 暂停恢复取消 / 人工介入 / 实时看板

> 上述为历史交付记录。实际代码在后续开发中已推进至 Phase 2、Phase 3，
> 但本文档此前未同步更新 —— 这也是本次改造中「文档与实现脱节」问题的来源之一。
