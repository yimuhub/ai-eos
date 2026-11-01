# 已知问题

> 最后更新：2026-11-01。2026-10-31 完成五视角审查（[审查报告](审查报告_AI-EOS_2026-10-31.md)）的
> 阶段 0 冲刺与达到 9 分的多轮并行升级：越权收口、群聊并发与配额、工作台长任务、安装可续装、
> request_id、Alembic 基线、指标、通知、会话管理、⌘K、License/席位/账单、评测集、性能预算、
> 部署与备份；2026-11-01 第五至十一波补齐 License 全局只读、租户矩阵扩展、前端数据层迁移、
> 演示交付物、交付单验收、行业包、本地语义检索、IM webhook、移动端 PWA、套餐/试用、BYOK 与
> `/api/v1` 别名。
> 明细见 [CHANGELOG](../CHANGELOG.md)「未发布」与
> [进度看板](进度看板_达到9分_2026-10-31.md)。
> 工程质量类的缺陷（未定义变量 / 静默吞异常 / 接口契约错配等）已在改造中清零，
> 记录见 [../UPGRADE_NOTES.md](../UPGRADE_NOTES.md)。以下为**仍然存在**的问题。

## 一、功能与体验

1. **前端数据层尚未全量迁移**：`useResource` 已覆盖 agent/任务/资产/模型/绩效/自动化/会议/
   交付/干预/项目/知识库/调度/公司/专家/治理/记忆/组织/反思/用量/工厂等约 25 页，
   提供并发去重、切公司跟随、回窗重取与写后刷新；其余约 21 页仍是手写
   `useEffect + setState`，局部不自动刷新的问题仍可能存在。命令面板已拆为异步包
   （首屏 120.0 KB），但全站 JS 仍用到预算的 97.9%，继续迁移时需同步拆包。

2. **导航信息架构未收敛**：一级业务页面 34 个、导航项 35 个平铺展示，
   新用户难以建立心智模型。建议按「组织 / 执行 / 成长 / 治理 / 资产」分为 5 组并默认折叠。
   2026-10-31 已给出静态原型（`apps/web/public/prototype/inbox-deliverable.html`，含 42 → 12
   的导航收敛方案），**代码未改**，待确认后再落。

3. **表单校验与危险操作确认不统一**：注册/登录页已做完整校验与限流，
   但其他页面的校验强度与二次确认策略不一致，需要统一梳理。

## 二、性能与扩展性

4. **单进程后台执行器**：`ThreadPoolExecutor(4)` 仅适合开发环境。
   2026-10-14 整改已堵住引擎泄漏（finally dispose）并按任务类型拆分超时
   （会议 600s / 任务默认 7200s 可配），但**任务持久化仍未做**，生产建议换 Celery / Temporal。

5. **Redis 未深度使用**：2026-10-14 起登录限流优先走 Redis（无 Redis 回退内存）；
   缓存与任务队列仍未启用。

## 三、安全与部署

6. **权限模型为公司级粗粒度**：缺「谁可以批准什么」的矩阵。
   跨租户越权（IDOR）已在改造中补齐并加了测试守护（2026-10-31 再收 performance 三端点与
   reflection 审批），但**同一公司内的角色区分与多成员共享**仍缺失。

7. **生产环境安全门禁已就位，但需人工确认**：
   `JWT_SECRET` 仍为占位值或 `DEBUG=true` 时，`AIEOS_ENV=production` 会**拒绝启动**
   （改造新增）。请用 `openssl rand -hex 32` 生成真实密钥。

## 四、工程质量（下一步方向）

8. **尚未用真实模型跑通端到端主链路**：`tools/e2e_smoke.py` 已用本地假模型跑通 38 项断言，
   支持 `DATABASE_URL` 覆盖并接入 CI（SQLite + PostgreSQL 16 job）；但仍没有用**真实模型密钥**
   跑通「建公司 → 下达目标 → 员工执行 → 产出报告」。这是判断「能否直接去用」的最后一道关，
   **建议优先做**。

9. **功能广度远超可交付深度**：403 个 API 路径 / 107 张表。
   建议收敛到 3–4 条真正跑通的完整链路，把深度做出来，而不是继续铺广度。

10. **既有库不会自动获得新唯一约束**：`chat_groups(company_id, preset_id)` 的唯一约束只在新建
    库时由 `create_all` 生效（运行时不做迁移）；老库靠 ensure 的 runtime lock 兜底。引入 Alembic
    基线后统一补齐。

11. **License 更新无自助界面**：过期/签名错误时所有业务写请求已被全局中间件拦截（403
    `license_read_only`），但更换 License 仍需替换 `apps/api/license.json` 或环境变量并重启；
    没有「上传/粘贴 License」的管理界面，小白用户无法在应用内完成续期。

12. **部署产物尚未本机验证**：`docker-compose.yml` 与两个 Dockerfile 已完成静态检查，但本机
    Docker daemon（colima）未运行、且未安装 compose 子命令，未实际 `docker compose up`；
    GitHub Actions 的 Postgres job、CodeQL、Dependabot 也待首次运行结果确认
    （本地 SQLite / 本地 PG 已全绿）。

13. **评测基线未建立**：评测器与 60 条种子任务（5 类 × 12）可离线自测，但还没有用真实
    模型跑出基线分数，随版本回归的真实通过率/平均分仍为空。

14. **可观测性缺开箱看板**：RED/LLM/队列指标与 [SLO 告警规则](../deploy/prometheus/alerts.yml)
    已就绪，但没有 Grafana dashboard JSON；自建 Prometheus 需要自行搭面板。

15. **证据链只覆盖联网搜索**：私聊里 `web_search` 的来源会进入引用面板；知识库 RAG 召回的
    文档来源尚未标注（需要检索层回传命中文档 id）。

16. **模型「已就绪」与连通性（已修复）**：2026-11-01 浏览器走查曾发现，向导第 4 步连通测试
    失败（HTTP 401）后 `GET /models/diagnostics` 仍返回 `ready: true`。现已把测试结果写回
    `Model.config`（`last_test_ok` / `last_test_at` / 截断后的 `last_test_error`），诊断只把
    「密钥就位且未明确失败」的模型算 ready；失败时 `hint` 直接给出原因并引导重新测试。
    回归：`test_onboarding_chain.py::test_failed_connectivity_test_marks_model_not_ready`。

17. **`response_model` 只覆盖核心路由，历史端点仍是裸 `dict`**：统一错误信封
    （`{detail, code, message, request_id}`）、`/api/v1` 版本别名均已落地；核心业务路由
    （员工/任务/项目/交付/评审/调度/技能/学习/绩效/反思）的 2xx JSON 操作已有
    **81/90 = 90%** 声明响应模型，但按全库 471 个操作算只有 **113/471 = 24%**。
    剩余历史端点（governance / war_room / kb2 / tokens / models_api 等）没有一次性补齐，
    原因是 `response_model` 会按模型裁剪返回字段，未逐个核对真实结构就声明会把多出来的
    字段静默丢掉。已加棘轮测试 `test_response_models.py`：核心路由不得低于 90%，全库
    声明数不得回退，新端点必须带模型；剩余部分按模块续补。

18. **覆盖率口径已修正，实测 69.8%**：此前报告的 47.9% 是测量错误——SQLAlchemy async
    在 await 边界交给 greenlet，coverage 不配 `concurrency = ["greenlet", "thread"]`
    会系统性丢追踪（同一文件 67% vs 99%）。修正后后端全量套件 **69.8%** 行覆盖
    （21822 行，缺 6584），门槛 40 → 65，CI 同步。未覆盖集中在历史引擎
    （training/team_builder）与未走到的异常分支，继续按模块补测。

19. **Web Push 已实现，但缺真实设备验证**：`/m` + service worker 已能订阅/退订并接收推送，
    订阅表、VAPID 环境变量、404/410 清理与事务后派发均已落地；要真正收到通知仍需在部署环境
    配置 `AIEOS_VAPID_PRIVATE_KEY` / `AIEOS_VAPID_PUBLIC_KEY`（HTTPS），并用真机验证。未配置时
    接口返回 `not_configured`，前端保持轮询，不会报错。

20. **Lighthouse 首屏门禁已接入**：`apps/web/lighthouserc.json` + CI `lighthouse` job，
    断言 LCP ≤1.5s、TBT ≤200ms、CLS ≤0.1、性能分 ≥90（3 次取中位数）。本机实测
    LCP 0.5–0.6s / TBT 0ms / CLS 0.005；GitHub runner 首次运行结果待观察。

21. **多端已读已同步，写冲突提示仍未做**：会话已读位置已落库并跨设备合并（只增不减），
    桌面与手机不再各记各的；但带正文的评论/备注仍没有版本号或冲突提示，极端情况下后写覆盖
    先写（审批走 CAS、交付验收幂等，风险主要在评论正文）。

## 五、已过时条目（此前记录，现已不成立）

- ~~**web_search 为骨架**，返回空结果占位~~ → 已实现真实搜索（Bing 中国，HTML 解析，无需 API key）
- ~~**测试账号** `boss@aieos.dev` / `Passw0rd!`~~ → **该账号在代码库中从未被创建**，
  README 中的相关说明已删除；现改为引导用户自行注册
- ~~前端 8 页面~~ → 现为 34 个一级业务页面
- ~~后端 38 端点 / 18 张表~~ → 现为 354 端点 / 94 张表
- ~~前端大量 `any`~~ → 已收敛到仅剩 `catch` 子句与 LLM 自由结构字段

## 六、2026-10-14 整改升级已修复

- ~~Pause 只在 checkpoint 生效且无 UX 提示~~ → 暂停按钮旁已加「暂停将在当前步骤完成后生效」提示；
  `resume_execution` 改为真重跑（从 checkpoint 恢复），不再是状态翻转
- ~~`usage_ctx` 从不 reset、training/factory 丢计量维度~~ → 改为上下文管理器（退出 reset）；
  training / factory / meeting / chat 补齐 agent / company 维度
- ~~`record_token_usage` 中途 commit 破坏事务原子性~~ → 只 add 不 commit，提交权交还调用方
- ~~JWT 无刷新机制~~ → RefreshToken 表 + `POST /auth/refresh`（轮换）+ `POST /auth/logout`（吊销），
  库里只存 SHA-256 哈希；前端 401 自动刷新
- ~~无成功率度量~~ → 失败调用记入 token 流水（`success=False`），`model_calls.success_rate` 口径修正
- ~~后台任务 600 秒死亡线~~ → 超时按任务类型拆分（`AIEOS_TASK_TIMEOUT_S`，默认 7200s），超时先写事件告知用户
- ~~FAILED 任务无重试~~ → 新增 `POST /tasks/{tid}/retry`（FAILED→PLANNING→RUNNING）+ 前端重试按钮
- ~~重启后 RUNNING 任务变僵尸~~ → lifespan 加 `recover_stuck_tasks()`（超 30 分钟无心跳标 FAILED + 写事件）
- ~~静默吞异常护栏有行尾注释绕行盲区~~ → `test_code_quality.py` 升级为 AST 级检测
- ~~`plugins/skills/` 假插件化~~ → `manifest.json` 真动态加载；`PROVIDERS` 支持 `plugins/providers/` 动态加载
- ~~`departments_batch` cascade 名不副实~~ → 真递归删除（已核查外键安全）
- ~~资产存绝对路径、kb 上传与截屏放 /tmp~~ → 相对路径存储（兼容旧绝对路径）；上传与截屏目录移出 /tmp
- ~~`/health/db` 异常仍返 200~~ → 异常返 HTTP 503
- ~~无 LICENSE~~ → BSL 1.1（BUSL-1.1）已补，`pyproject.toml` / `package.json` 同步标识
- ~~便携启动器在新机器跑不起来~~ → `start-mac.sh`（cd 到 apps/api、自动装依赖、日志追加、启动前快照）、
  `start-win.bat`（纯 batch 重写、补 `sqlite+aiosqlite:///` 前缀）已修；`.env.example` 中文模板已补；账号文档三处收敛
- ~~compose 非生产就绪~~ → 生产化重写（api/web 服务与 Dockerfile、restart/healthcheck、secrets、redis 持久化卷、端口绑 127.0.0.1）
- ~~无备份 / 无升级 SOP / 默认网关数据流向未告知~~ → 新增 `docs/BACKUP_RUNBOOK.md`、`docs/UPGRADE_SOP.md`、`docs/DATA_FLOW.md`
- ~~向导「隐藏第四步」（建员工不绑模型）~~ → 建员工自动绑定公司首选模型；新增免 Key 纯前端演示模式
