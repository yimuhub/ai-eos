#!/usr/bin/env bash
# AI-EOS SQLite 备份脚本
#
# 产出：
#   <out>/aieos-backup-YYYYmmdd-HHMMSS/
#     ├── aieos.db          # sqlite3 .backup 一致性快照（无 sqlite3 时退化为 cp）
#     ├── api.env           # apps/api/.env 的副本（0600，只在本地保留，绝不打印内容）
#     ├── jwt_secret.key    # 存在才备份（0600）；丢了会导致所有登录态失效
#     └── manifest.json     # 版本、时间、文件清单、sha256、来源库
#   以及同目录下的 aieos-backup-YYYYmmdd-HHMMSS.tar.gz（显式文件清单打包）
#
# 安全约束：
# - 只读真实库，不改动、不迁移、不删除；
# - 备份目录与文件默认 0700/0600；
# - 不在 stdout/stderr 输出 .env、JWT 密钥内容。
#
# 用法：
#   ./tools/backup.sh
#   ./tools/backup.sh --out /path/to/backups
#   ./tools/backup.sh --db /tmp/fake/aieos.db --env /tmp/fake/.env --out /tmp/bk
#
# 退出码：0 成功；非 0 失败（并清理未完成的临时目录）。

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/../aieos-backups"
DB_PATH="$REPO_ROOT/apps/api/data/aieos.db"
ENV_PATH="$REPO_ROOT/apps/api/.env"
ENV_EXPLICIT=0
MAKE_TAR=1

usage() {
  cat <<'EOF'
用法: tools/backup.sh [options]

  --out DIR     备份输出目录（默认：仓库同级 ../aieos-backups）
  --db PATH     SQLite 数据库路径（默认：apps/api/data/aieos.db）
  --env PATH    .env 路径（默认：apps/api/.env）
  --no-tar      只生成目录，不生成 .tar.gz
  -h, --help    显示帮助

说明：
  - 相对路径按仓库根目录解析；
  - 有 sqlite3 时用 .backup 做在线一致性快照；没有时要求无 -wal/-shm 写入；
  - 备份完成后可用 tools/restore.sh --from <目录或 .tar.gz> 恢复。
EOF
}

die()  { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '[backup] %s\n' "$*"; }
warn() { printf '[backup] WARN: %s\n' "$*" >&2; }

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
    --out)      [ "$#" -ge 2 ] || die "--out 需要一个目录"; OUT_DIR="$2"; shift 2 ;;
    --out=*)    OUT_DIR="${1#*=}"; shift ;;
    --db)       [ "$#" -ge 2 ] || die "--db 需要一个路径"; DB_PATH="$2"; shift 2 ;;
    --db=*)     DB_PATH="${1#*=}"; shift ;;
    --env)      [ "$#" -ge 2 ] || die "--env 需要一个路径"; ENV_PATH="$2"; ENV_EXPLICIT=1; shift 2 ;;
    --env=*)    ENV_PATH="${1#*=}"; ENV_EXPLICIT=1; shift ;;
    --no-tar)   MAKE_TAR=0; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "未知参数：$1（--help 查看用法）" ;;
  esac
done

OUT_DIR="$(abspath "$OUT_DIR")"
DB_PATH="$(abspath "$DB_PATH")"
ENV_PATH="$(abspath "$ENV_PATH")"

[ -f "$DB_PATH" ] || die "数据库不存在：$DB_PATH"
[ -r "$DB_PATH" ] || die "数据库不可读：$DB_PATH"
DB_BYTES="$(wc -c < "$DB_PATH" | tr -d ' ')"
[ "$DB_BYTES" -gt 0 ] || die "数据库是空文件：$DB_PATH"

if [ "$ENV_EXPLICIT" = "1" ] && [ ! -f "$ENV_PATH" ]; then
  die "--env 指定的文件不存在：$ENV_PATH"
fi

mkdir -p "$OUT_DIR" || die "无法创建输出目录：$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
chmod 700 "$OUT_DIR" 2>/dev/null || true

STAMP="$(date +%Y%m%d-%H%M%S)"
FINAL="$OUT_DIR/aieos-backup-$STAMP"
if [ -e "$FINAL" ]; then
  FINAL="$FINAL-$$"
fi
STAGE="$(mktemp -d "$OUT_DIR/.aieos-backup-$STAMP.tmp.XXXXXX")" \
  || die "无法在 $OUT_DIR 下创建临时目录"

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    [ -d "$STAGE" ] && rm -rf "$STAGE" 2>/dev/null || true
    if [ -n "${ARCHIVE:-}" ] && [ -f "$ARCHIVE" ]; then
      rm -f "$ARCHIVE" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------- 数据库快照
if command -v sqlite3 >/dev/null 2>&1; then
  info "使用 sqlite3 .backup 创建一致性快照"
  # SQLite dot 命令的路径参数无法安全转义单引号/换行；这类路径改用
  # Python sqlite3 的 backup API（底层同样是 .backup 机制，可任意路径）。
  case "$STAGE" in
    *"'"*|*$'\n'*)
      info "备份路径含单引号或换行，改用 Python sqlite3 backup API"
      python3 - "$DB_PATH" "$STAGE/aieos.db" <<'PY' \
        || die "sqlite3 backup API 失败（数据库可能被锁；请确认服务状态后重试）"
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1], timeout=5)
try:
    dst = sqlite3.connect(sys.argv[2])
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
PY
      DB_METHOD="python sqlite3 backup API"
      ;;
    *)
      sqlite3 "$DB_PATH" ".timeout 5000" ".backup '$STAGE/aieos.db'" \
        || die "sqlite3 .backup 失败（数据库可能被锁；请确认服务状态后重试）"
      DB_METHOD="sqlite3 .backup"
      ;;
  esac
  DB_CHECK="$(sqlite3 "$STAGE/aieos.db" 'PRAGMA integrity_check;' 2>&1 | tr -d '\r' || true)"
  [ "$DB_CHECK" = "ok" ] || die "备份库完整性校验失败：$DB_CHECK"
  # integrity_check 以 WAL 模式打开快照库时会生成 -wal/-shm 旁路文件。
  # 快照库应保持单文件：先确认 WAL 已清空，再删除旁路文件，
  # 否则 manifest 的 files 清单会与显式打包内容不一致。
  if [ -s "$STAGE/aieos.db-wal" ]; then
    die "备份库仍有未合并的 WAL（aieos.db-wal 非空），拒绝继续"
  fi
  rm -f "$STAGE/aieos.db-wal" "$STAGE/aieos.db-shm" "$STAGE/aieos.db-journal"
  info "备份库 integrity_check = ok"
else
  warn "未安装 sqlite3，退化为文件复制"
  if [ -s "${DB_PATH}-wal" ] || [ -s "${DB_PATH}-shm" ]; then
    die "检测到非空 ${DB_PATH}-wal / -shm，数据库可能正在写入。请先停止服务，或安装 sqlite3 后做在线热备"
  fi
  cp -p "$DB_PATH" "$STAGE/aieos.db" || die "复制数据库失败"
  DB_METHOD="cp（sqlite3 不可用）"
fi
chmod 600 "$STAGE/aieos.db"

# ---------------------------------------------------------------- .env
ENV_STATE="absent"
if [ -f "$ENV_PATH" ]; then
  cp -p "$ENV_PATH" "$STAGE/api.env" || die "复制 .env 失败"
  chmod 600 "$STAGE/api.env"
  ENV_STATE="included"
  info ".env 已备份（内容不打印）"
else
  warn ".env 不存在，跳过：$ENV_PATH"
fi

# ---------------------------------------------------------------- JWT 密钥
# 只从数据库同目录或显式 AIEOS_JWT_SECRET_FILE 取，避免自定义 --db 时误拷真实密钥。
JWT_STATE="absent"
JWT_SOURCE=""
if [ -n "${AIEOS_JWT_SECRET_FILE:-}" ]; then
  if [ -d "${AIEOS_JWT_SECRET_FILE}" ]; then
    JWT_SOURCE="${AIEOS_JWT_SECRET_FILE%/}/jwt_secret.key"
  elif [ -f "${AIEOS_JWT_SECRET_FILE}" ]; then
    JWT_SOURCE="${AIEOS_JWT_SECRET_FILE}"
  fi
fi
if [ -z "$JWT_SOURCE" ] && [ -f "$(dirname "$DB_PATH")/jwt_secret.key" ]; then
  JWT_SOURCE="$(dirname "$DB_PATH")/jwt_secret.key"
fi
if [ -n "$JWT_SOURCE" ] && [ -f "$JWT_SOURCE" ]; then
  cp -p "$JWT_SOURCE" "$STAGE/jwt_secret.key" || die "复制 JWT 密钥文件失败"
  chmod 600 "$STAGE/jwt_secret.key"
  JWT_STATE="included"
  info "JWT 密钥文件已备份（内容不打印）"
fi

# ---------------------------------------------------------------- manifest
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$REPO_ROOT/apps/api/app/version.py" 2>/dev/null | head -n 1)"
[ -n "$VERSION" ] || VERSION="unknown"
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
[ -n "$GIT_COMMIT" ] || GIT_COMMIT="unknown"

AIEOS_VERSION="$VERSION" \
AIEOS_GIT_COMMIT="$GIT_COMMIT" \
AIEOS_SOURCE_DB="$DB_PATH" \
AIEOS_DB_METHOD="$DB_METHOD" \
AIEOS_ENV_STATE="$ENV_STATE" \
AIEOS_JWT_STATE="$JWT_STATE" \
python3 - "$STAGE" <<'PY' || die "生成 manifest.json 失败"
import hashlib
import json
import os
import socket
import sys
import time

stage = sys.argv[1]
files = []
db_sha = ""
db_bytes = 0

for name in sorted(os.listdir(stage)):
    path = os.path.join(stage, name)
    if not os.path.isfile(path):
        continue
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    st = os.stat(path)
    item = {
        "name": name,
        "sha256": h.hexdigest(),
        "bytes": st.st_size,
        "mode": oct(st.st_mode & 0o777),
    }
    files.append(item)
    if name == "aieos.db":
        db_sha = item["sha256"]
        db_bytes = item["bytes"]

if not db_sha:
    sys.exit("aieos.db 不在临时目录中")

manifest = {
    "schema_version": 1,
    "version": os.environ.get("AIEOS_VERSION", "unknown"),
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "created_at_unix": int(time.time()),
    "hostname": socket.gethostname(),
    "repo_git_commit": os.environ.get("AIEOS_GIT_COMMIT", "unknown"),
    "source_database": {
        "path": os.environ.get("AIEOS_SOURCE_DB", ""),
        "method": os.environ.get("AIEOS_DB_METHOD", ""),
    },
    "database": {
        "file": "aieos.db",
        "sha256": db_sha,
        "bytes": db_bytes,
    },
    "env_file": os.environ.get("AIEOS_ENV_STATE", "absent"),
    "jwt_secret_file": os.environ.get("AIEOS_JWT_STATE", "absent"),
    "files": files,
    "notes": [
        "api.env 与 jwt_secret.key 含敏感信息，权限 0600，请与数据库分开离线保管。",
        "manifest.json 自身不列入 files（无法自哈希）；恢复时按 files 逐项校验。",
    ],
}

with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.chmod(os.path.join(stage, "manifest.json"), 0o600)
PY
DB_SHA="$(python3 - "$STAGE/manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.load(fh)["database"]["sha256"])
PY
)"
chmod 600 "$STAGE"/*

# ---------------------------------------------------------------- 显式清单打包
ARCHIVE=""
ARCHIVE_SHA=""
if [ "$MAKE_TAR" = "1" ]; then
  ARCHIVE="$OUT_DIR/aieos-backup-$STAMP.tar.gz"
  if [ -e "$ARCHIVE" ]; then
    ARCHIVE="$OUT_DIR/aieos-backup-$STAMP-$$.tar.gz"
  fi

  # 关键：BSD tar 的 --exclude 不可靠，这里只传显式文件名，不靠排除规则。
  TAR_FILES=(aieos.db)
  if [ -f "$STAGE/api.env" ]; then
    TAR_FILES+=(api.env)
  fi
  if [ -f "$STAGE/jwt_secret.key" ]; then
    TAR_FILES+=(jwt_secret.key)
  fi
  TAR_FILES+=(manifest.json)

  tar -czf "$ARCHIVE" -C "$STAGE" "${TAR_FILES[@]}" || die "tar 打包失败"
  chmod 600 "$ARCHIVE"
  ARCHIVE_SHA="$(hash_file "$ARCHIVE")"
fi

mv "$STAGE" "$FINAL" || die "无法把临时目录重命名为最终备份目录"
trap - EXIT

info "备份完成：$FINAL"
info "数据库快照方式：$DB_METHOD"
info "数据库 SHA256：$DB_SHA"
if [ -n "$ARCHIVE" ]; then
  info "归档文件：$ARCHIVE"
  info "归档 SHA256：$ARCHIVE_SHA"
fi
info "请把 $FINAL（或归档）复制到与生产机不同的位置离线保管"
