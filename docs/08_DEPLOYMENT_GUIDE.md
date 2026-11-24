# 部署指南

## 依赖环境
macOS + colima (Docker) + uv + Node.js 20+

## 启动步骤

```bash
# 1. 基础设施（PostgreSQL 16 + Redis 7）
cd ai-eos && docker compose -f docker/docker-compose.yml up -d

# 2. 数据库迁移（SQLite / PostgreSQL 使用同一 Alembic 链）
cd apps/api && uv sync
uv run python -m app.db.migrations upgrade
uv run python -m app.db.migrations check

# 3. 后端（开发热重载）
uv run uvicorn app.main:app --port 8000 --reload

# 4. 前端
cd ../web && npm install && npm run dev   # http://localhost:3000
```

> SQLite 便携首启会自动执行迁移并校验；PostgreSQL 启动只校验，部署前必须显式升级。
> 运行时不再 `create_all` 或手写补列。无版本号老库只有在结构与模型完全一致时才允许
> 登记基线，不完整老库会被拒绝。详见 `docs/数据库迁移.md`。

## 环境变量 (.env)
DATABASE_URL / REDIS_URL / JWT_SECRET /
OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_MODEL

## 让 AI 员工操作另一台电脑（远程执行设备）

后端这台机器之外想再挂一台（比如老板的 MacBook），在「设备操作台」建设备拿接入码，
目标机器上跑 sidecar——单文件、纯标准库，不用装依赖也不用 clone 仓库：

```bash
python3 apps/device_agent/aieos_sidecar.py enroll \
    --server http://<后端机器内网IP>:8000 --code <接入码> --allow-root ~/work/project
python3 apps/device_agent/aieos_sidecar.py run
```

参数、两把锁（服务端策略 + 本机护栏）与排错见 [apps/device_agent/README.md](../apps/device_agent/README.md)。

## 注意事项
- 国内网络拉取镜像用 docker.m.daocloud.io 前缀
- bcrypt 需锁 <4 版本（passlib 兼容）
- 生产部署: JWT_SECRET 必须更换；前端 CORS allow_origins 收紧
