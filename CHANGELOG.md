# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式。
完整的工程质量改造记录见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)。

## [未发布]

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

### 已新增

- **测试体系重建**：113 → 117 个离线用例（原 29 个「测试」文件里 28 个无法运行）
  - 覆盖认证与限流、账号枚举防护、跨租户隔离、沙箱与命令注入、接口契约、代码质量护栏
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
