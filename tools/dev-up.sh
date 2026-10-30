#!/usr/bin/env bash
# 本地开发服务常驻：API :8000 + Web :3100
# 用法：tools/dev-up.sh            启动（已在跑就跳过）
#      tools/dev-up.sh --restart 先停再启
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/logs/run"
mkdir -p "$RUN"

API_PORT=8000
WEB_PORT=3100
API_PID="$RUN/api-$API_PORT.pid"
WEB_PID="$RUN/web-$WEB_PORT.pid"

http_ok() { curl -sf -m 2 "$1" >/dev/null 2>&1; }
port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }
wait_http() {
  local url="$1" name="$2" i
  for i in $(seq 1 45); do
    http_ok "$url" && { echo "  ✔ $name $url"; return 0; }
    sleep 1
  done
  echo "  ✖ $name 启动超时，看日志：$RUN" >&2
  return 1
}
stop_port() {
  local port="$1" name="$2" p
  while read -r p; do
    [ -n "$p" ] || continue
    kill "$p" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true
    echo "  ■ 已停止 $name (pid $p)"
  done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
}

if [ "${1:-}" = "--restart" ]; then
  rm -f "$API_PID" "$WEB_PID"
  stop_port "$WEB_PORT" "Web :$WEB_PORT"
  stop_port "$API_PORT" "API :$API_PORT"
fi

# ---- API ----
if http_ok "http://127.0.0.1:$API_PORT/health"; then
  port_pid "$API_PORT" > "$API_PID"
  echo "  · API 已在运行 (pid $(cat "$API_PID"))"
else
  echo "▶ 启动 API :$API_PORT"
  cd "$ROOT/apps/api"
  # .env 里的 DATABASE_URL 指向本机 PG（角色可能尚未创建），这里显式走仓库内的 SQLite
  DATABASE_URL="sqlite+aiosqlite:///./data/aieos.db" \
    nohup ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" \
    >> "$RUN/api-$API_PORT.log" 2>&1 &
  echo $! > "$API_PID"
fi

# ---- Web ----
if http_ok "http://127.0.0.1:$WEB_PORT/login"; then
  port_pid "$WEB_PORT" > "$WEB_PID"
  echo "  · Web 已在运行 (pid $(cat "$WEB_PID"))"
else
  echo "▶ 启动 Web :$WEB_PORT"
  cd "$ROOT/apps/web"
  nohup ./node_modules/.bin/next dev -p "$WEB_PORT" \
    >> "$RUN/web-$WEB_PORT.log" 2>&1 &
  echo $! > "$WEB_PID"
fi

wait_http "http://127.0.0.1:$API_PORT/health" "API"
wait_http "http://127.0.0.1:$WEB_PORT/login" "Web"
echo "完成。日志：$RUN（api-$API_PORT.log / web-$WEB_PORT.log）"
