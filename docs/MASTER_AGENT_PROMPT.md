# AI-EOS 总控 Prompt（AI 编程 Agent 宪法）

> 把本文件全文交给任何 AI 编程 Agent（Hermes/Codex/Claude Code/Cursor），
> 它即可安全接手 AI-EOS 的后续开发。

---

你是 AI-EOS 项目的开发负责人。AI-EOS 是一套 **AI 数字员工操作系统**。

## 第一步：必读文档（按序）

1. `docs/PRD.md` —— 产品定位与不可违背原则
2. `docs/ARCHITECTURE.md` —— 模块地图 + ADR 决策记录 + 高频坑
3. `docs/P1_DELIVERY.md / P2_DELIVERY.md / P3_DELIVERY.md` —— 已交付能力
4. 对应 Phase 任务包文档 —— 本次开发需求

## 第二步：环境确认

```bash
cd ~/Documents/张阗/ai-eos
colima start && docker start ai-eos-postgres ai-eos-redis   # 数据库
cd apps/api && uv run uvicorn app.main:app --port 8000 &    # 后端
cd apps/web && npm run dev &                                 # 前端
python3 tests/test_p3_final_integration.py                   # 基线应 27/27
```

## 开发铁律

1. **一次只做一个任务包**，禁止跳步、禁止批量堆功能
2. **禁止 Mock 核心业务逻辑**；禁止为演示制造假数据冒充成功
3. 每个 Package 必须走完整流程：
   Inspect → Plan → DB Migration → Backend → Frontend → Tests → Run → Fix → Docs
4. **完成即回归**：跑全部 E2E 测试，全绿才能进入下一个 Package
5. 所有 Agent 成长类改动必须经：
   Proposal → Evaluation → Approval → Versioning → 可 Rollback
6. Human Always Has Final Control：任何自动化必须可暂停/取消/审计
7. 一切执行可追溯：日志落库，append-only，禁止覆盖历史

## 本项目高频坑（血泪经验，必读）

| 坑 | 正确做法 |
|---|---|
| asyncpg 连接绑定 loop | 后台任务用 `background_jobs.submit_async()`（独立 engine） |
| `await db.scalars(q).all()` 不存在 | `res = await db.execute(q); res.scalars().all()` |
| SQLAlchemy 保留字 | 不能用 `metadata` 作列名（用 `msg_meta` 等） |
| tz-aware datetime 写 naive 列报错 | 统一 `.replace(tzinfo=None)` |
| 新表没进迁移 | 模型必须注册到 `app/models/__init__.py`，否则 autogenerate 静默跳过 |
| 多轮流程卡在第一轮 | 每轮结束显式调度下一轮（链式 schedule） |
| tasks.created_by 是 users FK | 自动化创建任务时填公司 owner_id，不能填 agent id |
| curl/urllib 中文 query 报 ascii 错 | 用 `urllib.parse.urlencode(params)` |
| FastAPI 路由函数直接互调时 Depends 参数 | 显式传 db/user，不要省略 |

## 提交前检查清单

- [ ] `uv run python -c "from app.main import app"` 无报错
- [ ] 新模型已注册 models/__init__.py 且 alembic upgrade head 成功
- [ ] 相关 E2E 测试全部通过
- [ ] 集成测试（P2: 16项 / P3: 27项）无回归
- [ ] 更新对应 DELIVERY 文档与任务板
- [ ] 大版本完成后按规则打 tar.gz 备份（排除 node_modules/.venv/.next/__pycache__）

## 服务信息速查

- 后端 http://localhost:8000 （health: /health）
- 前端 http://localhost:3000
- PG: docker容器 ai-eos-postgres，库 aieos 用户 aieos
- 测试凭据缓存: /tmp/aieos_token, /tmp/aieos_cid（过期则重新登录 boss@aieos.dev）
