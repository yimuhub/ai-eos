# AI-EOS 后端（FastAPI）生产镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# healthcheck 用 curl 探 /health
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 拷后端源码并安装依赖（pyproject.toml 的 [project] 即安装清单）
COPY apps/api/ ./
RUN pip install --no-cache-dir .

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
