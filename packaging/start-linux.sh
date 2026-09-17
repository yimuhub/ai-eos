#!/bin/bash
# AI-EOS 便携包 — Linux 双击启动器
# 依赖系统环境：python3 + node（通常已预装：apt install python3 nodejs npm）
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

API_PORT=8001
FRONT_PORT=8000
WORK_DIR="$(pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()    { echo -e "${GREEN}[AI-EOS]${NC} $*"; }
warn()   { echo -e "${YELLOW}[AI-EOS]${NC} $*"; }
err()    { echo -e "${RED}[AI-EOS]${NC} $*"; }

mkdir -p data storage logs

check_python() { command -v python3 >/dev/null 2>&1 || { err "python3 未安装: sudo apt install python3"; exit 1; }; }
check_node()   { command -v node >/dev/null 2>&1   || { err "node 未安装: sudo apt install nodejs npm"; exit 1; }; }

start_api() {
  log "启动后端 (SQLite + FastAPI) 端口 $API_PORT ..."
  export PYTHONUNBUFFERED=1
  export DATABASE_URL="sqlite+aiosqlite:///$WORK_DIR/data/aieos.db"
  python3 "$WORK_DIR/apps/api/app/initializers/__init__.py" "$WORK_DIR/data/aieos.db" 2>/dev/null || true
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port $API_PORT --log-level info \
    > "$WORK_DIR/logs/api.log" 2>&1 &
  log "后端 PID: $!"
}

wait_api_ready() {
  for i in $(seq 1 30); do
    sleep 1
    curl -s --max-time 2 "http://localhost:$API_PORT/health" >/dev/null 2>&1 && { log "后端就绪"; return 0; }
    warn "等待后端... ($i/30)"
  done
  err "后端启动超时，请查看 logs/api.log"; return 1
}

start_front() {
  log "启动前端 (Next.js) 端口 $FRONT_PORT ..."
  export NEXT_PUBLIC_API_URL="http://localhost:$API_PORT"
  cd "$WORK_DIR/apps/web"
  if [ ! -d ".next" ]; then
    log "首次启动，构建前端（约 1-2 分钟）..."
    npm install --prefer-offline 2>&1 | tail -1
    npx next build --no-lint 2>&1 | tail -3
  fi
  npx next start -p $FRONT_PORT --log-level warn \
    > "$WORK_DIR/logs/front.log" 2>&1 &
  log "前端 PID: $!"
}

wait_front_ready() {
  for i in $(seq 1 20); do
    sleep 1
    curl -s --max-time 2 "http://localhost:$FRONT_PORT/" >/dev/null 2>&1 && { log "前端就绪"; return 0; }
    warn "等待前端... ($i/20)"
  done
  err "前端启动超时，请查看 logs/front.log"; return 1
}

# 停止旧进程
for p in $(lsof -ti :$API_PORT 2>/dev/null); do kill $p 2>/dev/null; done
for p in $(lsof -ti :$FRONT_PORT 2>/dev/null); do kill $p 2>/dev/null; done

check_python
check_node
start_api
wait_api_ready || exit 1
start_front
wait_front_ready || exit 1

log "=========================================="
log "  AI-EOS 已启动！"
log "  打开浏览器: http://localhost:$FRONT_PORT"
log "  后端端口:   $API_PORT (API)"
log "  停止命令:   关闭此终端，或 kill $(pgrep -f 'uvicorn app.main')"
log "  日志路径:   $WORK_DIR/logs/"
log "=========================================="

open "http://localhost:$FRONT_PORT" 2>/dev/null || xdg-open "http://localhost:$FRONT_PORT" 2>/dev/null || true

trap 'log "正在关闭..."; pkill -f "uvicorn app.main"; pkill -f "next start"; exit 0' INT TERM
wait
