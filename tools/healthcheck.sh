#!/usr/bin/env bash
# AI-EOS 交付健康检查（只读，不启动/重启任何服务）
#
# 检查项：
#   1. API  GET /health                    → 200 且 body.status=ok
#   2. API  GET /metrics/prometheus        → 鉴权行为（配了 token 时无 token 必须 401/403）
#   3. Web  GET /                          → 200
#   4. 磁盘剩余空间                        → 不低于阈值
#
# 输出 PASS / FAIL / SKIP 汇总；存在 FAIL 时退出码为 1，否则 0。
#
# 用法：
#   ./tools/healthcheck.sh
#   ./tools/healthcheck.sh --api-url http://127.0.0.1:8000 --web-url http://127.0.0.1:3000
#   ./tools/healthcheck.sh --skip-web --min-disk-mb 2048
#
# 默认端口与单机启动脚本一致：API 8001，Web 8000（Docker Compose 为 8001 / 3000）。
# METRICS_TOKEN 从 --env-file（默认 apps/api/.env）读取，绝不打印其值。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

API_URL="${AIEOS_API_URL:-http://127.0.0.1:8001}"
WEB_URL="${AIEOS_WEB_URL:-http://127.0.0.1:8000}"
ENV_FILE="$REPO_ROOT/apps/api/.env"
MIN_DISK_MB=1024
TIMEOUT=5
DISK_PATH="$REPO_ROOT"
CHECK_API=1
CHECK_WEB=1

usage() {
  cat <<'EOF'
用法: tools/healthcheck.sh [options]

  --api-url URL      API 地址（默认 http://127.0.0.1:8001）
  --web-url URL      Web 地址（默认 http://127.0.0.1:8000）
  --env-file PATH    读取 METRICS_TOKEN 的 .env（默认 apps/api/.env）
  --min-disk-mb N    磁盘剩余下限，单位 MB（默认 1024）
  --timeout N        HTTP 超时秒数（默认 5）
  --disk-path PATH   检查哪个路径所在磁盘（默认仓库根目录）
  --skip-api         跳过 API 检查
  --skip-web         跳过 Web 检查
  -h, --help         显示帮助

退出码：0 全部 PASS/SKIP；1 存在 FAIL。
EOF
}

PASS_N=0
FAIL_N=0
SKIP_N=0

pass() { PASS_N=$((PASS_N + 1)); printf 'PASS  %-22s %s\n' "$1" "$2"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf 'FAIL  %-22s %s\n' "$1" "$2"; }
skip() { SKIP_N=$((SKIP_N + 1)); printf 'SKIP  %-22s %s\n' "$1" "$2"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api-url)      [ "$#" -ge 2 ] || { echo "缺少 --api-url 参数" >&2; exit 2; }; API_URL="$2"; shift 2 ;;
    --api-url=*)    API_URL="${1#*=}"; shift ;;
    --web-url)      [ "$#" -ge 2 ] || { echo "缺少 --web-url 参数" >&2; exit 2; }; WEB_URL="$2"; shift 2 ;;
    --web-url=*)    WEB_URL="${1#*=}"; shift ;;
    --env-file)     [ "$#" -ge 2 ] || { echo "缺少 --env-file 参数" >&2; exit 2; }; ENV_FILE="$2"; shift 2 ;;
    --env-file=*)   ENV_FILE="${1#*=}"; shift ;;
    --min-disk-mb)  [ "$#" -ge 2 ] || { echo "缺少 --min-disk-mb 参数" >&2; exit 2; }; MIN_DISK_MB="$2"; shift 2 ;;
    --min-disk-mb=*) MIN_DISK_MB="${1#*=}"; shift ;;
    --timeout)      [ "$#" -ge 2 ] || { echo "缺少 --timeout 参数" >&2; exit 2; }; TIMEOUT="$2"; shift 2 ;;
    --timeout=*)    TIMEOUT="${1#*=}"; shift ;;
    --disk-path)    [ "$#" -ge 2 ] || { echo "缺少 --disk-path 参数" >&2; exit 2; }; DISK_PATH="$2"; shift 2 ;;
    --disk-path=*)  DISK_PATH="${1#*=}"; shift ;;
    --skip-api)     CHECK_API=0; shift ;;
    --skip-web)     CHECK_WEB=0; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              echo "未知参数：$1（--help 查看用法）" >&2; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 || { echo "FAIL  缺少 curl，无法做 HTTP 检查" >&2; exit 1; }

BODY="$(mktemp "${TMPDIR:-/tmp}/aieos-health.XXXXXX")" || exit 1
cleanup() { rm -f "$BODY" 2>/dev/null || true; }
trap cleanup EXIT

http_code() {
  # $1=url，其余参数透传给 curl；失败时返回 000（只输出一份）
  local url="$1" out
  shift
  out="$(curl -sS -o "$BODY" -w '%{http_code}' --max-time "$TIMEOUT" "$@" "$url" 2>/dev/null)"
  [ -n "$out" ] || out="000"
  printf '%s' "$out"
}

read_env_value() {
  # 从 .env 读一个键，去掉首尾空白与成对引号；找不到输出空串。绝不回显。
  local key="$1" line val
  [ -f "$ENV_FILE" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  val="${line#*=}"
  val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}

API_URL="${API_URL%/}"
WEB_URL="${WEB_URL%/}"
# 环境变量优先；否则从 .env 读取。两者都不打印。
METRICS_TOKEN="${METRICS_TOKEN:-$(read_env_value METRICS_TOKEN)}"

# ---------------------------------------------------------------- 1. API /health
if [ "$CHECK_API" = "1" ]; then
  code="$(http_code "$API_URL/health")"
  if [ "$code" = "200" ] && grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' "$BODY"; then
    pass "API /health" "HTTP 200，status=ok"
  else
    snippet="$(head -c 200 "$BODY" 2>/dev/null | tr '\n' ' ' || true)"
    fail "API /health" "HTTP ${code}（期望 200 + status=ok）body=${snippet}"
  fi

  # -------------------------------------------------------------- 2. 指标鉴权
  code_no_token="$(http_code "$API_URL/metrics/prometheus")"
  if [ -n "$METRICS_TOKEN" ]; then
    case "$code_no_token" in
      401|403)
        pass "指标鉴权（无 token）" "HTTP ${code_no_token}，已被拒绝"
        ;;
      *)
        fail "指标鉴权（无 token）" "HTTP ${code_no_token}（期望 401/403；检查 DEBUG 是否误开、METRICS_TOKEN 是否生效）"
        ;;
    esac

    code_token="$(printf 'header = "Authorization: Bearer %s"\n' "$METRICS_TOKEN" \
      | curl -sS -K - -o "$BODY" -w '%{http_code}' --max-time "$TIMEOUT" \
        "$API_URL/metrics/prometheus" 2>/dev/null)"
    [ -n "$code_token" ] || code_token="000"
    if [ "$code_token" = "200" ] && [ -s "$BODY" ]; then
      if grep -q 'aieos_' "$BODY"; then
        pass "指标鉴权（带 token）" "HTTP 200，返回 aieos_* 指标"
      else
        pass "指标鉴权（带 token）" "HTTP 200，响应非空（未发现 aieos_ 前缀，可能指标已改名）"
      fi
    else
      fail "指标鉴权（带 token）" "HTTP ${code_token}（期望 200 且响应非空）"
    fi
  else
    case "$code_no_token" in
      200)
        skip "指标鉴权（无 token）" "未配置 METRICS_TOKEN；DEBUG/回环放行（生产请配置 token）"
        ;;
      401|403|404)
        pass "指标鉴权（无 token）" "HTTP ${code_no_token}，端点受保护/隐藏（未配置 token）"
        ;;
      *)
        fail "指标鉴权（无 token）" "HTTP ${code_no_token}（期望 200/401/403/404）"
        ;;
    esac
  fi
fi

# ---------------------------------------------------------------- 3. Web 首页
if [ "$CHECK_WEB" = "1" ]; then
  code="$(http_code "$WEB_URL/")"
  if [ "$code" = "200" ] && [ -s "$BODY" ]; then
    pass "Web /" "HTTP 200，页面非空"
  else
    fail "Web /" "HTTP ${code}（期望 200 且页面非空）"
  fi
fi

# ---------------------------------------------------------------- 4. 磁盘
if [ -d "$DISK_PATH" ]; then
  avail_kb="$(df -Pk "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $4}')"
  if printf '%s' "$avail_kb" | grep -Eq '^[0-9]+$'; then
    min_kb=$((MIN_DISK_MB * 1024))
    avail_mb=$((avail_kb / 1024))
    if [ "$avail_kb" -ge "$min_kb" ]; then
      pass "磁盘剩余" "${avail_mb} MB ≥ ${MIN_DISK_MB} MB（$DISK_PATH）"
    else
      fail "磁盘剩余" "${avail_mb} MB < ${MIN_DISK_MB} MB（$DISK_PATH）"
    fi
  else
    fail "磁盘剩余" "无法解析 df 输出（$DISK_PATH）"
  fi
else
  fail "磁盘剩余" "路径不存在：$DISK_PATH"
fi

# ---------------------------------------------------------------- 汇总
printf '\n汇总：%d PASS, %d FAIL, %d SKIP\n' "$PASS_N" "$FAIL_N" "$SKIP_N"
if [ "$FAIL_N" -gt 0 ]; then
  exit 1
fi
exit 0
