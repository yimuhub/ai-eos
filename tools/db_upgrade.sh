#!/usr/bin/env bash
# AI-EOS schema 升级脚本（Alembic）
#
# 为什么要有它：Alembic 本身能跑，但"裸跑 alembic upgrade head"很容易在
# 服务器上误伤错误的库（shell 里残留的 DATABASE_URL、cwd 不对、多套 .env）。
# 这里强制两件事：
#   1) 目标库必须显式给出（DATABASE_URL 或 --db），没有默认值可用；
#   2) 执行前打印"要动哪个库、当前/目标版本"，并留 3 秒反悔时间。
#
# 用法：
#   tools/db_upgrade.sh --db /tmp/drill/aieos.db              # 升到 head
#   tools/db_upgrade.sh --db /tmp/drill/aieos.db --to 0001    # 升到指定版本
#   DATABASE_URL=postgresql+asyncpg://... tools/db_upgrade.sh
#   AIEOS_YES=1 tools/db_upgrade.sh --db ...                  # 跳过 3 秒等待
#   tools/db_upgrade.sh --db ... --sql                        # 只打印 SQL，不执行
#
# 退出码：0 成功；2 参数错误；其他非 0 为 alembic/环境错误。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
API_DIR="$REPO_ROOT/apps/api"
DEFAULT_DB="$API_DIR/data/aieos.db"

DB_ARG=""
TARGET_REV="head"
SQL_ONLY=0

usage() {
  cat <<EOF
用法: tools/db_upgrade.sh (--db PATH | DATABASE_URL=URL) [--to REV] [--sql]

  --db PATH      SQLite 文件路径（相对路径按当前目录解析）
  --to REV       目标版本，默认 head
  --sql          离线模式：只输出 SQL，不连库、不执行
  -h, --help     显示本帮助

必须显式给出 --db 或 DATABASE_URL；脚本不会自行猜测数据库。
仓库默认库位置是 $DEFAULT_DB（仅作提示，不会自动使用）。
设置 AIEOS_YES=1 可跳过 3 秒确认等待（CI/自动化用）。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "错误：--db 需要路径参数" >&2; exit 2; }
      DB_ARG="$2"; shift 2 ;;
    --to|--revision)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "错误：--to 需要版本号" >&2; exit 2; }
      TARGET_REV="$2"; shift 2 ;;
    --sql) SQL_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数 $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# 1) 解析目标库（显式优先；绝无默认值）
# ---------------------------------------------------------------------------
if [[ -n "${DATABASE_URL:-}" ]]; then
  TARGET_URL="$DATABASE_URL"
  DISPLAY="DATABASE_URL=$DATABASE_URL"
elif [[ -n "$DB_ARG" ]]; then
  DB_PATH="$DB_ARG"
  if [[ "$DB_PATH" != /* ]]; then
    DB_PATH="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"
  fi
  if [[ ! -d "$(dirname "$DB_PATH")" ]]; then
    echo "错误：目录不存在：$(dirname "$DB_PATH")" >&2
    exit 2
  fi
  TARGET_URL="sqlite:///$DB_PATH"
  DISPLAY="$DB_PATH"
else
  echo "错误：必须显式指定目标库 —— 传 --db PATH 或设置 DATABASE_URL。" >&2
  echo "      仓库默认库 $DEFAULT_DB 不会被自动使用（防误伤）。" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 2) Python 解释器：优先项目 venv，其次 PATH
# ---------------------------------------------------------------------------
if [[ -x "$API_DIR/.venv/bin/python" ]]; then
  PY="$API_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="${PYTHON:-python3}"
else
  echo "错误：找不到 python3，也没有 $API_DIR/.venv" >&2
  exit 2
fi

if ! "$PY" -c "import alembic" >/dev/null 2>&1; then
  echo "错误：当前解释器 $PY 没有安装 alembic。" >&2
  echo "      在 apps/api 下执行：uv sync（或 uv pip install alembic）" >&2
  exit 2
fi

# 只取 "0001 (head)" 这种版本行。
# env.py 会 import 路由模块，应用日志（带时间戳/request-id）也走 stdout，
# 直接 tail -1 会拿到日志行而不是版本号。
current_rev() {
  "$PY" -m alembic -c "$API_DIR/alembic.ini" -x db_url="$TARGET_URL" \
    current 2>/dev/null | grep -E '^[0-9A-Za-z_]+( \(head\))?$' | tail -n 1 || true
}

# ---------------------------------------------------------------------------
# 3) 打印将操作的库 + 当前版本，留 3 秒反悔
# ---------------------------------------------------------------------------
STATE="（不存在，将新建空库）"
if [[ -n "$DB_ARG" && -f "$DB_ARG" ]]; then
  STATE="（已存在，$(du -h "$DB_ARG" | cut -f1)）"
elif [[ -n "${DATABASE_URL:-}" ]]; then
  STATE="（外部 URL，容量未知）"
fi

echo "============================================================"
echo " AI-EOS schema 升级（alembic upgrade $TARGET_REV）"
echo " 目标库 : $DISPLAY"
echo " 状态   : $STATE"
if [[ "$SQL_ONLY" == "1" ]]; then
  echo " 模式   : 离线，仅输出 SQL（不改库）"
else
  echo " 模式   : 在线执行（会修改 schema，建议先备份：tools/backup.sh）"
fi
echo "============================================================"

if [[ "$SQL_ONLY" != "1" ]]; then
  CURRENT="$(current_rev)"
  echo " 当前版本: ${CURRENT:-（空库/未 stamp）}"
  echo " 目标版本: $TARGET_REV"
  if [[ "${AIEOS_YES:-0}" != "1" ]]; then
    echo " 3 秒后开始，Ctrl-C 可取消（AIEOS_YES=1 跳过等待）"
    sleep 3
  fi
fi

# ---------------------------------------------------------------------------
# 4) 执行
# ---------------------------------------------------------------------------
cd "$API_DIR"
if [[ "$SQL_ONLY" == "1" ]]; then
  "$PY" -m alembic -c alembic.ini -x db_url="$TARGET_URL" upgrade "$TARGET_REV" --sql
  exit 0
fi

"$PY" -m alembic -c alembic.ini -x db_url="$TARGET_URL" upgrade "$TARGET_REV"

echo "------------------------------------------------------------"
echo " 升级完成，当前版本：$(current_rev)"
echo " 注意：运行时仍以 create_all + ensure_columns 兜底（见 docs/数据库迁移.md）"
