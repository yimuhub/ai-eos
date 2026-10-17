# AI-EOS 生产升级 SOP

> 标准流程：备份 → 升级 → 冒烟 →（异常）回滚。
> 任何一步失败都停下来，先回滚再排查，不带病上线。

## 0. 升级前检查

- [ ] 当前版本已记录：`git rev-parse --short HEAD`（或 CHANGELOG 中的版本号）
- [ ] 目标版本的 CHANGELOG 已读，确认无破坏性变更
- [ ] 升级窗口已通知用户（预计停机时长）

## 1. 备份（按 docs/BACKUP_RUNBOOK.md）

- [ ] 数据库备份完成（PG：`pg_dump`；SQLite：文件拷贝）
- [ ] `storage/` 已同步到备份位置
- [ ] `.env`（`apps/api/.env` / `docker/.env`）已离线保管
- [ ] 备份文件可读、非空（`ls -lh` 确认大小合理）

## 2. 执行升级

### Docker 部署
```bash
cd docker
docker compose pull          # 拉新镜像（如用构建：--build）
docker compose up -d --build
# 数据库迁移（PostgreSQL）：alembic 是 schema 变更的正道
docker compose exec api alembic upgrade head
```

### 便携包 / 源码部署
```bash
# 1. 停服  2. 替换代码  3. 装依赖  4. 启动
# SQLite 无需 alembic：启动时自动建表（create_all）+ 幂等补列（ensure_columns）
# ensure_columns 只是兜底，PostgreSQL 生产库请以 alembic upgrade head 为准
```

> 注意：`ensure_columns` 只加可空列、不改类型；PostgreSQL 上它与 Alembic 双轨并行，
> 升级后若出现 "no such column"，优先检查 alembic 是否执行到 head。

## 3. 冒烟测试（缺一不可）

| # | 检查项 | 期望 |
|---|--------|------|
| 1 | `GET /health` | 200 `{"status":"ok"}` |
| 2 | `GET /health/db` | 200（503 说明数据库没连上） |
| 3 | 登录页注册/登录 | 正常进入指挥中心 |
| 4 | 指挥中心 / 任务页 | 加载无报错 |
| 5 | 下达一条测试任务 | 可创建、可执行 |
| 6 | `GET /metrics/prometheus` | 200，`aieos_db_up 1` |
| 7 | 日志检查 | `logs/ai-eos.log` 无连续 ERROR |

生产环境另查：`AIEOS_ENV=production` 且 `JWT_SECRET` 非默认值、`DEBUG=false`
（否则服务会拒绝启动，这是预期的安全门禁）。

## 4. 回滚

冒烟任一项失败：

```bash
# 1. 停服
docker compose down  # 或关闭启动器
# 2. 代码回退到升级前版本
git checkout <升级前的 commit>
# 3. 按 BACKUP_RUNBOOK 第 4 节恢复数据库与 storage
# 4. 重启并重新冒烟（第 3 节）
```

## 5. 升级后

- [ ] 冒烟全绿，通知用户升级完成
- [ ] 保留升级记录：版本、时间、执行人、备份位置
- [ ] 观察 24 小时：错误率、磁盘、`/metrics/prometheus` 的 `aieos_db_up`
