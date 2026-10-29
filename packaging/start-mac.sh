#!/bin/bash
# AI-EOS 便携包 — macOS 双击启动器
# 依赖系统环境：python3 + node（macOS 常见；首次提示安装时会自动拉起 brew 安装）
# 架构：FastAPI (SQLite) 单进程 + Next.js 生产前端（同端口 8000，FastAPI 托管静态文件）
# 数据：data/  SQLite 数据库 + storage/ 文件产出物

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 端口
API_PORT=8001
FRONT_PORT=8000
WORK_DIR="$(pwd)"

# 颜色提示
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log() { echo -e "${GREEN}[AI-EOS]${NC} $*"; }
warn() { echo -e "${YELLOW}[AI-EOS]${NC} $*"; }
err()  { echo -e "${RED}[AI-EOS]${NC} $*"; }

# 确保目录结构
mkdir -p data storage logs

# 环境检测
check_python() {
  if ! command -v python3 &>/dev/null; then
    err "Python3 未安装。macOS 推荐执行：brew install python3"
    exit 1
  fi
  log "Python: $(python3 --version)"
}

check_node() {
  if ! command -v node &>/dev/null; then
    err "Node.js 未安装。macOS 推荐执行：brew install node"
    exit 1
  fi
  log "Node:   $(node -v)"
}

# 首次运行自动装依赖：检测不到 fastapi 说明依赖从未安装
ensure_deps() {
  if python3 -c "import fastapi" 2>/dev/null; then
    return 0
  fi
  warn "未检测到 Python 依赖，首次运行自动安装（约 1-3 分钟）..."
  cd "$WORK_DIR/apps/api"
  if command -v uv &>/dev/null; then
    log "使用 uv 安装依赖..."
    uv sync 2>&1 | tail -5
  else
    log "使用 pip 安装依赖..."
    pip3 install . 2>&1 | tail -5
  fi
  if ! python3 -c "import fastapi" 2>/dev/null; then
    err "依赖安装失败，请查看上方输出后重试"
    exit 1
  fi
  log "依赖安装完成"
}

# 启动前自动快照 data/（只保留最近 5 份）
snapshot_data() {
  [ -d "$WORK_DIR/data" ] || return 0
  local ts old
  ts=$(date +%Y%m%d-%H%M%S)
  log "快照数据目录 data/ → data-backup-$ts"
  cp -R "$WORK_DIR/data" "$WORK_DIR/data-backup-$ts"
  # 只保留最近 5 份快照
  ls -dt "$WORK_DIR"/data-backup-* 2>/dev/null | tail -n +6 | while read -r old; do
    rm -rf "$old" && log "清理旧快照：$(basename "$old")"
  done || true
}

# 启动后端（uvicorn）
start_api() {
  log "启动后端 (SQLite + FastAPI) 端口 $API_PORT ..."
  export PYTHONUNBUFFERED=1
  export DATABASE_URL="sqlite+aiosqlite:///$WORK_DIR/data/aieos.db"
  # 密钥引导：JWT_SECRET 留空时后端会自动生成随机密钥并落盘到这里。
  # 显式指定路径，避免 uvicorn 的工作目录（apps/api）把密钥写进包外。
  # ⚠️ 备份 data/ 时请连同该文件一起备份：丢了 = 所有登录态失效。
  export AIEOS_JWT_SECRET_FILE="$WORK_DIR/data/jwt_secret.key"
  ensure_deps
  # 首次启动建表：用内置脚本
  python3 "$WORK_DIR/apps/api/app/initializers/__init__.py" "$WORK_DIR/data/aieos.db" 2>&1 | tail -20
  # uvicorn 必须以 apps/api 为工作目录启动，app.main 才能被 import
  cd "$WORK_DIR/apps/api"
  # 启动 uvicorn（日志追加 >>，保留历史，不再截断）
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port $API_PORT --log-level info \
    >> "$WORK_DIR/logs/api.log" 2>&1 &
  API_PID=$!
  log "后端 PID: $API_PID"
}

# 等待后端就绪
wait_api_ready() {
  for i in $(seq 1 30); do
    sleep 1
    if curl -s --max-time 2 "http://localhost:$API_PORT/health" >/dev/null 2>&1; then
      log "后端就绪"
      return 0
    fi
    warn "等待后端... ($i/30)"
  done
  err "后端启动超时，请查看 logs/api.log"
  return 1
}

# 启动前端（next start，绑定到 8000 并反向代理 /api → 8001）
start_front() {
  log "启动前端 (Next.js) 端口 $FRONT_PORT ..."
  export NEXT_PUBLIC_API_URL="http://localhost:$API_PORT"
  cd "$WORK_DIR/apps/web"
  # 首次启动从源码 build 一次；若 .next 存在直接 start
  if [ ! -d ".next" ]; then
    log "首次启动，构建前端（约 1-2 分钟）..."
    npm install --prefer-offline 2>&1 | tail -1
    npx next build --no-lint 2>&1 | tail -3
  fi
  npx next start -p $FRONT_PORT --log-level warn \
    >> "$WORK_DIR/logs/front.log" 2>&1 &
  FRONT_PID=$!
  log "前端 PID: $FRONT_PID"
}

wait_front_ready() {
  for i in $(seq 1 20); do
    sleep 1
    if curl -s --max-time 2 "http://localhost:$FRONT_PORT/" >/dev/null 2>&1; then
      log "前端就绪"
      return 0
    fi
    warn "等待前端... ($i/20)"
  done
  err "前端启动超时，请查看 logs/front.log"
  return 1
}

# 主流程
main() {
  check_python
  check_node

  # 停止旧进程（同一端口）
  local old
  old=$(lsof -ti :$API_PORT 2>/dev/null)
  [ -n "$old" ] && kill $old 2>/dev/null && log "关闭旧后端 ($old)"
  old=$(lsof -ti :$FRONT_PORT 2>/dev/null)
  [ -n "$old" ] && kill $old 2>/dev/null && log "关闭旧前端 ($old)"

  snapshot_data

  start_api
  wait_api_ready || exit 1

  start_front
  wait_front_ready || exit 1

  log "=========================================="
  log "  AI-EOS 已启动！"
  log "  打开浏览器: http://localhost:$FRONT_PORT"
  log "  后端端口:   $API_PORT (API)"
  log "  停止命令:   关闭此终端，或执行 kill $API_PID $FRONT_PID"
  log "  日志路径:   $WORK_DIR/logs/"
  log "=========================================="

  # 自动打开浏览器（macOS 专有）
  open "http://localhost:$FRONT_PORT"

  # 等待 Ctrl-C
  trap 'log "正在关闭..."; kill $API_PID $FRONT_PID 2>/dev/null; exit 0' INT TERM
  wait
}

main "$@"
