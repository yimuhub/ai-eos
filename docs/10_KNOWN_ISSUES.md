# 已知问题

> 最后更新：2026-10-14。2026-10-14 完成五视角审查整改升级，修复条目见文末第六节。
> 工程质量类的缺陷（未定义变量 / 静默吞异常 / 接口契约错配等）已在改造中清零，
> 记录见 [../UPGRADE_NOTES.md](../UPGRADE_NOTES.md)。以下为**仍然存在**的问题。

## 一、功能与体验

1. **前端局部不自动刷新**：组织架构添加部门、协作页新建会话后需手动刷新。
   数据已正确落库，仅列表未重新拉取。改造中已为调度、员工详情等页面补了轮询与加载态，
   但**尚未统一**（缺 SWR / React Query 这类数据层）。

2. **导航信息架构未收敛**：一级业务页面 34 个、导航项 35 个平铺展示，
   新用户难以建立心智模型。建议按「组织 / 执行 / 成长 / 治理 / 资产」分为 5 组并默认折叠。

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
   跨租户越权（IDOR）已在改造中补齐并加了测试守护，但**同一公司内的角色区分**仍缺失。

7. **生产环境安全门禁已就位，但需人工确认**：
   `JWT_SECRET` 仍为占位值或 `DEBUG=true` 时，`AIEOS_ENV=production` 会**拒绝启动**
   （改造新增）。请用 `openssl rand -hex 32` 生成真实密钥。

## 四、工程质量（下一步方向）

8. **尚未做真实的端到端试跑**：`tools/e2e_smoke.py` 已用本地假模型跑通 38 项断言，
   但还没有用真实模型密钥跑通「建公司 → 下达目标 → 员工执行 → 产出报告」这条主链路。
   这是判断「能否直接去用」的最后一道关，**建议优先做**。

9. **功能广度远超可交付深度**：354 个端点 / 94 张表。
   建议收敛到 3–4 条真正跑通的完整链路，把深度做出来，而不是继续铺广度。

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
