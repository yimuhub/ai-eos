#!/usr/bin/env bash
# AI-EOS SQLite 恢复脚本
#
# 流程（顺序不可颠倒）：
#   1. 解包 / 读取备份目录；
#   2. 按 manifest.json 逐文件校验 sha256（任一不符即中止）；
#   3. 确认目标库当前没有进程占用 / 非空 WAL；
#   4. 二次确认（交互输入 RESTORE，或 AIEOS_YES=1）；
#   5. 自动调用 tools/backup.sh 备份「当前」数据库；
#   6. 原子替换目标库，再次校验 sha256 与 integrity_check；
#   7. 打印迁移/重启/健康检查命令，但**不自动重启服务**。
#
# 约束：
# - 只覆盖 --db 指定的数据库；默认 apps/api/data/aieos.db；
# - 默认不恢复 .env / JWT 密钥（需要时显式加 --with-env / --with-jwt）；
# - 不删除备份源，不迁移真实库以外的数据。
#
# 用法：
#   ./tools/restore.sh --from ../aieos-backups/aieos-backup-20260101-030000
#   ./tools/restore.sh --from /path/aieos-backup-....tar.gz --db /tmp/fake/aieos.db --env /tmp/fake/.env
#   AIEOS_YES=1 ./tools/restore.sh --from ... --with-env --with-jwt
#
# 退出码：0 成功；非 0 失败（校验/前置备份阶段失败时不会覆盖目标库）。

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FROM=""
DB_PATH="$REPO_ROOT/apps/api/data/aieos.db"
ENV_PATH="$REPO_ROOT/apps/api/.env"
PRE_OUT="$REPO_ROOT/../aieos-backups"
WITH_ENV=0
WITH_JWT=0
FORCE=0

usage() {
  cat <<'EOF'
用法: tools/restore.sh --from DIR_OR_TAR [options]

  --from PATH          备份目录或 aieos-backup-*.tar.gz（必填）
  --db PATH            目标 SQLite 数据库（默认 apps/api/data/aieos.db）
  --env PATH           目标 .env（默认 apps/api/.env；仅 --with-env 时覆盖）
  --pre-backup-out DIR 恢复前自动备份的输出目录（默认 ../aieos-backups）
  --with-env           同时恢复备份中的 api.env（会覆盖目标 .env）
  --with-jwt           同时恢复 jwt_secret.key
  --force              跳过数据库占用 / WAL 检查（确认服务已停时使用）
  --yes                等同于 AIEOS_YES=1，跳过交互确认（脚本化场景）
  -h, --help           显示帮助

二次确认：交互输入 RESTORE，或设置 AIEOS_YES=1。
恢复后不会自动重启服务，命令见脚本末尾输出。
EOF
}

die()  { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '[restore] %s\n' "$*"; }
warn() { printf '[restore] WARN: %s\n' "$*" >&2; }

abspath() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s/%s' "$REPO_ROOT" "$1" ;;
  esac
}

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    python3 - "$1" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --from)            [ "$#" -ge 2 ] || die "--from 需要一个路径"; FROM="$2"; shift 2 ;;
    --from=*)          FROM="${1#*=}"; shift ;;
    --db)              [ "$#" -ge 2 ] || die "--db 需要一个路径"; DB_PATH="$2"; shift 2 ;;
    --db=*)            DB_PATH="${1#*=}"; shift ;;
    --env)             [ "$#" -ge 2 ] || die "--env 需要一个路径"; ENV_PATH="$2"; shift 2 ;;
    --env=*)           ENV_PATH="${1#*=}"; shift ;;
    --pre-backup-out)  [ "$#" -ge 2 ] || die "--pre-backup-out 需要一个目录"; PRE_OUT="$2"; shift 2 ;;
    --pre-backup-out=*) PRE_OUT="${1#*=}"; shift ;;
    --with-env)        WITH_ENV=1; shift ;;
    --with-jwt)        WITH_JWT=1; shift ;;
    --force)           FORCE=1; shift ;;
    --yes)             export AIEOS_YES=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *)                 die "未知参数：$1（--help 查看用法）" ;;
  esac
done

[ -n "$FROM" ] || die "必须指定 --from（备份目录或 .tar.gz）"

FROM="$(abspath "$FROM")"
if [ "$FROM" != "/" ]; then
  FROM="${FROM%/}"
fi
DB_PATH="$(abspath "$DB_PATH")"
ENV_PATH="$(abspath "$ENV_PATH")"
PRE_OUT="$(abspath "$PRE_OUT")"

[ -e "$FROM" ] || die "备份不存在：$FROM"
[ -f "$SCRIPT_DIR/backup.sh" ] || die "找不到 $SCRIPT_DIR/backup.sh"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/aieos-restore.XXXXXX")" || die "无法创建临时目录"
TMP_TARGET=""
cleanup() {
  rm -rf "$TMP" 2>/dev/null || true
  if [ -n "$TMP_TARGET" ] && [ -e "$TMP_TARGET" ]; then
    rm -f "$TMP_TARGET" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------- 定位备份内容
if [ -d "$FROM" ]; then
  SRC="$FROM"
elif [ -f "$FROM" ]; then
  case "$FROM" in
    *.tar.gz|*.tgz)
      info "解包归档：$FROM"
      tar -xzf "$FROM" -C "$TMP" || die "解包失败：$FROM"
      SRC="$TMP"
      ;;
    *)
      die "--from 需要备份目录或 .tar.gz 文件：$FROM"
      ;;
  esac
else
  die "--from 既不是目录也不是文件：$FROM"
fi

[ -f "$SRC/manifest.json" ] || die "备份缺少 manifest.json：$SRC"
[ -f "$SRC/aieos.db" ] || die "备份缺少 aieos.db：$SRC"
if [ "$WITH_ENV" = "1" ] && [ ! -f "$SRC/api.env" ]; then
  die "备份没有 api.env，无法 --with-env（尚未覆盖任何文件）"
fi
if [ "$WITH_JWT" = "1" ] && [ ! -f "$SRC/jwt_secret.key" ]; then
  die "备份没有 jwt_secret.key，无法 --with-jwt（尚未覆盖任何文件）"
fi

# ---------------------------------------------------------------- sha256 校验
VERIFY_OUT="$(python3 - "$SRC" "$TMP/expected_db.sha256" <<'PY'
import hashlib
import json
import os
import sys

src, out_path = sys.argv[1], sys.argv[2]
manifest_path = os.path.join(src, "manifest.json")

try:
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"manifest.json 无法解析：{exc}")

files = manifest.get("files")
if not isinstance(files, list) or not files:
    sys.exit("manifest.json 缺少 files 清单")

seen = set()
sidecars = {"aieos.db-wal", "aieos.db-shm", "aieos.db-journal"}
for item in files:
    name = item.get("name", "")
    expected = item.get("sha256", "")
    if not name or os.path.basename(name) != name:
        sys.exit(f"manifest 中存在非法文件名：{name!r}")
    if name in sidecars:
        # 旧版备份可能把 integrity_check 产生的旁路文件列入清单；
        # 它们不是恢复所需内容，目录备份与 tar 归档都可能不含，直接忽略。
        continue
    if name in seen:
        sys.exit(f"manifest 中文件重复：{name}")
    seen.add(name)
    path = os.path.join(src, name)
    if not os.path.isfile(path):
        sys.exit(f"备份文件缺失：{name}")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        sys.exit(f"sha256 不匹配：{name}\n  期望 {expected}\n  实际 {actual}")

db_expected = (manifest.get("database") or {}).get("sha256", "")
if not db_expected:
    sys.exit("manifest.json 缺少 database.sha256")
actual_db = ""
for item in files:
    if item.get("name") == "aieos.db":
        actual_db = item.get("sha256", "")
        break
if actual_db != db_expected:
    sys.exit("manifest 的 database.sha256 与 files 清单不一致")

with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(db_expected)
print(len(files))
PY
)" || die "备份校验失败，未做任何覆盖"

FILE_COUNT="$(printf '%s' "$VERIFY_OUT" | tr -d '[:space:]')"
DB_SHA_EXPECTED="$(tr -d '[:space:]' < "$TMP/expected_db.sha256")"
[ -n "$DB_SHA_EXPECTED" ] || die "无法读取 manifest 中的数据库哈希"
info "备份校验通过：${FILE_COUNT} 个文件；数据库 sha256=$DB_SHA_EXPECTED"

# ---------------------------------------------------------------- 目标检查
TARGET_DIR="$(dirname "$DB_PATH")"
mkdir -p "$TARGET_DIR" || die "无法创建目标目录：$TARGET_DIR"
[ -w "$TARGET_DIR" ] || die "目标目录不可写：$TARGET_DIR"

if [ -e "${DB_PATH}-wal" ] || [ -e "${DB_PATH}-shm" ]; then
  if [ "$FORCE" != "1" ]; then
    die "检测到 ${DB_PATH}-wal/-shm，服务可能仍在运行。请先停服；确认已停可加 --force"
  fi
  warn "--force：将忽略 WAL/SHM 并清除它们（请确保服务已停止）"
fi

if command -v lsof >/dev/null 2>&1 && [ -e "$DB_PATH" ]; then
  if lsof "$DB_PATH" >/dev/null 2>&1; then
    if [ "$FORCE" != "1" ]; then
      die "数据库仍被进程占用（lsof 可见）：$DB_PATH。请先停服，或确认后加 --force"
    fi
    warn "--force：数据库仍被进程占用，继续恢复可能导致该进程写入混乱"
  fi
fi

# ---------------------------------------------------------------- 二次确认
printf '\n即将恢复：\n'
printf '  备份来源   : %s\n' "$SRC"
printf '  源数据库   : %s/aieos.db\n' "$SRC"
printf '  数据库哈希 : %s（已校验）\n' "$DB_SHA_EXPECTED"
printf '  目标数据库 : %s\n' "$DB_PATH"
if [ "$WITH_ENV" = "1" ]; then
  printf '  目标 .env  : %s（将被覆盖）\n' "$ENV_PATH"
fi
if [ "$WITH_JWT" = "1" ]; then
  printf '  JWT 密钥   : 将恢复到数据库同目录或 AIEOS_JWT_SECRET_FILE\n'
fi
printf '  恢复前备份 : 自动写入 %s\n' "$PRE_OUT"

if [ "${AIEOS_YES:-}" != "1" ]; then
  if [ ! -t 0 ]; then
    die "非交互环境必须设置 AIEOS_YES=1 才能恢复"
  fi
  printf '\n输入 RESTORE 确认覆盖（其他任何输入都会取消）：'
  IFS= read -r ANSWER || ANSWER=""
  [ "$ANSWER" = "RESTORE" ] || die "用户取消，未做任何覆盖"
fi

# ---------------------------------------------------------------- 恢复前自动备份
mkdir -p "$PRE_OUT" || die "无法创建恢复前备份目录：$PRE_OUT"
if [ -f "$DB_PATH" ]; then
  info "恢复前自动备份当前数据库..."
  BK_ARGS=(--out "$PRE_OUT" --db "$DB_PATH")
  if [ -f "$ENV_PATH" ]; then
    BK_ARGS+=(--env "$ENV_PATH")
  fi
  "$SCRIPT_DIR/backup.sh" "${BK_ARGS[@]}" \
    || die "恢复前自动备份失败，已中止（目标库未被覆盖）"
else
  warn "目标数据库不存在，跳过恢复前自动备份（没有当前数据可备份）"
fi

# ---------------------------------------------------------------- 原子替换
TMP_TARGET="$(mktemp "${DB_PATH}.restore.tmp.XXXXXX")" || die "无法在目标目录创建临时文件"
cp "$SRC/aieos.db" "$TMP_TARGET" || die "复制备份数据库到临时文件失败"
chmod 600 "$TMP_TARGET"

ACTUAL_TMP="$(hash_file "$TMP_TARGET")"
[ "$ACTUAL_TMP" = "$DB_SHA_EXPECTED" ] \
  || die "临时文件 sha256 与 manifest 不一致，已中止（目标库未被覆盖）"

mv -f "$TMP_TARGET" "$DB_PATH" || die "替换目标数据库失败"
TMP_TARGET=""
chmod 600 "$DB_PATH"

# 服务已停（或用户 --force）时，旧 WAL/journal 必须清掉，否则会回放到新库上
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm" "${DB_PATH}-journal"

ACTUAL_DB="$(hash_file "$DB_PATH")"
[ "$ACTUAL_DB" = "$DB_SHA_EXPECTED" ] \
  || die "恢复后 sha256 不一致（期望 $DB_SHA_EXPECTED，实际 $ACTUAL_DB）"

if command -v sqlite3 >/dev/null 2>&1; then
  CHECK="$(sqlite3 "$DB_PATH" 'PRAGMA integrity_check;' 2>&1 | tr -d '\r' || true)"
  [ "$CHECK" = "ok" ] || die "恢复后 integrity_check 失败：$CHECK"
fi

info "数据库已恢复并通过 sha256 校验：$DB_PATH"

# ---------------------------------------------------------------- 可选恢复配置
if [ "$WITH_ENV" = "1" ]; then
  mkdir -p "$(dirname "$ENV_PATH")" || die "无法创建 .env 目录"
  cp "$SRC/api.env" "$ENV_PATH" || die "恢复 .env 失败"
  chmod 600 "$ENV_PATH"
  info ".env 已恢复（内容不打印）：$ENV_PATH"
fi

if [ "$WITH_JWT" = "1" ]; then
  if [ -n "${AIEOS_JWT_SECRET_FILE:-}" ]; then
    JWT_TARGET="$AIEOS_JWT_SECRET_FILE"
  else
    JWT_TARGET="$(dirname "$DB_PATH")/jwt_secret.key"
  fi
  # 与 config.py 一致：环境变量指向目录时，落到目录下的 jwt_secret.key
  if [ -d "$JWT_TARGET" ]; then
    JWT_TARGET="${JWT_TARGET%/}/jwt_secret.key"
  fi
  mkdir -p "$(dirname "$JWT_TARGET")" || die "无法创建 JWT 密钥目录"
  cp "$SRC/jwt_secret.key" "$JWT_TARGET" || die "恢复 JWT 密钥失败"
  chmod 600 "$JWT_TARGET"
  info "JWT 密钥已恢复（内容不打印）：$JWT_TARGET"
fi

cat <<EOF

[restore] 完成。恢复前的库已自动备份到：$PRE_OUT
[restore] 本脚本不会自动重启服务。下一步：

  1) 数据库迁移（生产/PostgreSQL 必须；SQLite 建议执行）：
     cd "$REPO_ROOT/apps/api" && uv run alembic upgrade head

  2) 重启服务（二选一）：
     源码/便携包： "$REPO_ROOT/packaging/start-mac.sh"
                  "$REPO_ROOT/packaging/start-linux.sh"
     Docker     ： cd "$REPO_ROOT" && docker compose up -d

  3) 健康检查：
     "$REPO_ROOT/tools/healthcheck.sh"

  4) 看日志确认无连续 ERROR：
     tail -n 100 "$REPO_ROOT/apps/api/logs/"*.log 2>/dev/null || true
     Docker：docker compose logs --tail=100 api
EOF
