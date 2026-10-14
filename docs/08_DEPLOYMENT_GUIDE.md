# 部署指南

## 依赖环境
macOS + colima (Docker) + uv + Node.js 20+

## 启动步骤

```bash
# 1. 基础设施（PostgreSQL 16 + Redis 7）
cd ai-eos && docker compose -f docker/docker-compose.yml up -d

# 2. 数据库迁移
cd apps/api && uv sync && uv run alembic upgrade head

# 3. 后端（开发热重载）
uv run uvicorn app.main:app --port 8000 --reload

# 4. 前端
cd ../web && npm install && npm run dev   # http://localhost:3000
```

## 环境变量 (.env)
DATABASE_URL / REDIS_URL / JWT_SECRET /
OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_MODEL

## 注意事项
- 国内网络拉取镜像用 docker.m.daocloud.io 前缀
- bcrypt 需锁 <4 版本（passlib 兼容）
- 生产部署: JWT_SECRET 必须更换；前端 CORS allow_origins 收紧
