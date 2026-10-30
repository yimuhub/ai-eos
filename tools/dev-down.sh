#!/usr/bin/env bash
# 停止 tools/dev-up.sh 拉起的服务
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/logs/run"

for port in 3100 8000; do
  while read -r p; do
    [ -n "$p" ] || continue
    kill "$p" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true
    echo "■ 已停止 :$port (pid $p)"
  done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
done

rm -f "$RUN/api-8000.pid" "$RUN/web-3100.pid"
echo "done"
