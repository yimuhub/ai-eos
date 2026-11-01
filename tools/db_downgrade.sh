#!/usr/bin/env bash
# AI-EOS schema 回滚脚本（Alembic downgrade）
#
# 回滚是不可逆的数据操作：降到某个版本会执行该 revision 的 downgrade，
# 其中很可能包含 drop_table / drop_column。执行前必须：
#   1) 显式给出目标库（DATABASE_URL 或 --db），脚本不会猜；
#   2) 看清楚横幅里的"当前版本 → 目标版本"和 3 秒倒计时。
#
# 用法：
#   tools/db_downgrade.sh --db /tmp/drill/aieos.db            # 回滚到 base
#   tools/db_downgrade.sh --db /tmp/drill/aieos.db --to 0001
#   DATABASE_URL=postgresql+asyncpg://... tools/db_downgrade.sh --to -1
#   AIEOS_YES=1 tools/db_downgrade.sh --db /tmp/drill/aieos.db  # 跳过等待
#
# 退出码：0 成功；2 参数错误；其他非 0 为 alembic/环境错误。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
API_DIR="$REPO_ROOT/apps/api"
DEFAULT_DB="$API_DIR/data/aieos.db"

DB_ARG=""
TARGET_REV="base"

usage() {
  cat <<EOF
用法: tools/db_downgrade.sh (--db PATH | DATABASE_URL=URL) [--to REV]

  --db PATH      SQLite 文件路径（相对路径按当前目录解析）
  --to REV       目标版本，默认 base（= 删除全部业务表）
                 -1 表示只回退一个 revision
  -h, --help     显示本帮助

必须显式给出 --db 或 DATABASE_URL；脚本不会自行猜测数据库。
仓库默认库位置是 $DEFAULT_DB（仅作提示，不会自动使用）。
设置 AIEOS_YES=1 可跳过 3 秒确认等待（CI/自动化用）。
回滚前请先备份：tools/backup.sh。
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
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数 $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "${DATABASE_URL:-}" ]]; then
  TARGET_URL="$DATABASE_URL"
  DISPLAY="DATABASE_URL=$DATABASE_URL"
elif [[ -n "$DB_ARG" ]]; then
  DB_PATH="$DB_ARG"
  if [[ "$DB_PATH" != /* ]]; then
    DB_PATH="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"
  fi
  if [[ ! -f "$DB_PATH" ]]; then
    echo "错误：数据库文件不存在：$DB_PATH（回滚不会新建库）" >&2
    exit 2
  fi
  TARGET_URL="sqlite:///$DB_PATH"
  DISPLAY="$DB_PATH"
else
  echo "错误：必须显式指定目标库 —— 传 --db PATH 或设置 DATABASE_URL。" >&2
  echo "      仓库默认库 $DEFAULT_DB 不会被自动使用（防误伤）。" >&2
  exit 2
fi

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

# 只取 "0001 (head)" 这种版本行：env.py 会 import 路由模块，
# 应用日志也走 stdout，直接 tail -1 会拿到日志行。
current_rev() {
  "$PY" -m alembic -c "$API_DIR/alembic.ini" -x db_url="$TARGET_URL" \
    current 2>/dev/null | grep -E '^[0-9A-Za-z_]+( \(head\))?$' | tail -n 1 || true
}

CURRENT="$(current_rev)"

echo "============================================================"
echo " AI-EOS schema 回滚（alembic downgrade $TARGET_REV）"
echo " 目标库 : $DISPLAY"
echo " 当前版本: ${CURRENT:-（未 stamp，无法回滚）}"
echo " 目标版本: $TARGET_REV"
if [[ "$TARGET_REV" == "base" ]]; then
  echo " 警告   : base 会删除全部业务表，只保留 alembic_version！"
fi
echo " 建议   : 先备份 —— tools/backup.sh"
echo "============================================================"

if [[ -z "$CURRENT" ]]; then
  echo "错误：该库没有 alembic_version 记录，没有可回滚的版本。" >&2
  exit 2
fi

if [[ "${AIEOS_YES:-0}" != "1" ]]; then
  echo " 3 秒后开始，Ctrl-C 可取消（AIEOS_YES=1 跳过等待）"
  sleep 3
fi

cd "$API_DIR"
"$PY" -m alembic -c alembic.ini -x db_url="$TARGET_URL" downgrade "$TARGET_REV"

echo "------------------------------------------------------------"
echo " 回滚完成，当前版本：$(current_rev)"
