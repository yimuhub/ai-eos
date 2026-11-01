#!/usr/bin/env bash
# AI-EOS 代码仓库备份脚本（本地 + 云端双份的"本地"这一半）
#
# 产出（默认 ../aieos-backups/repo/ 下）：
#   aieos-repo-YYYYmmdd-HHMMSS.bundle        git bundle --all（全部分支/标签，可完整还原）
#   aieos-worktree-YYYYmmdd-HHMMSS.tar.gz    工作区快照（含未提交改动，排除依赖/构建/密钥）
#   repo-manifest-YYYYmmdd-HHMMSS.json       提交号、分支、sha256、文件大小
#   以及 tools/backup.sh 产出的数据库 + .env 备份（--no-data 可跳过）
#
# 为什么要两个都留：
# - bundle 只包含"已提交"的内容，靠它就能重建整个 git 历史；
# - 工作区快照包含"还没提交"的改动——密集开发期最怕的正是这部分丢失。
#
# 安全约束：
# - 工作区快照**排除** .env / *.key / *.pem / *credentials* / data / 打包产物；
# - 加 --encrypt 时，产物一律用 openssl AES-256 加密后再落盘，明文不保留；
#   口令只从环境变量 AIEOS_BACKUP_PASSPHRASE 读取，不回显、不写文件；
# - 保留份数有上限（默认 14），只删除本脚本自己产出的文件，不碰其他东西。
#
# 用法：
#   ./tools/backup_repo.sh
#   ./tools/backup_repo.sh --out /Volumes/BackupDisk/aieos
#   ./tools/backup_repo.sh --keep 30 --no-worktree
#   AIEOS_BACKUP_PASSPHRASE='...' ./tools/backup_repo.sh --encrypt --out ~/iCloud/aieos-backups
#
# 退出码：0 成功；非 0 失败（失败时会清理未完成的临时目录）。

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/../aieos-backups/repo"
KEEP=14
MAKE_WORKTREE=1
WITH_DATA=1
ENCRYPT=0
STAMP="$(date +%Y%m%d-%H%M%S)"

usage() {
  cat <<'EOF'
用法: tools/backup_repo.sh [options]

  --out DIR        输出目录（默认：仓库同级 ../aieos-backups/repo）
  --keep N         保留最近 N 份（默认 14，0 表示不清理）
  --no-worktree    只做 git bundle，不做工作区快照
  --no-data        不调用 tools/backup.sh（不备份数据库 / .env）
  --encrypt        用 AIEOS_BACKUP_PASSPHRASE 加密全部产物（云端同步前必选）
  -h, --help       显示帮助

说明：
  - 工作区快照排除 node_modules/.next/.venv/dist/data 等，且永远不包含密钥文件；
  - 加密口令只从环境变量读取，脚本不会打印它；
  - 恢复方法见 docs/BACKUP_RUNBOOK.md 第 6 节。
EOF
}

die()  { printf '[backup-repo] ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '[backup-repo] %s\n' "$*"; }
warn() { printf '[backup-repo] WARN: %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)        OUT_DIR="${2:?--out 需要目录参数}"; shift 2 ;;
    --keep)       KEEP="${2:?--keep 需要数字}"; shift 2 ;;
    --no-worktree) MAKE_WORKTREE=0; shift ;;
    --no-data)    WITH_DATA=0; shift ;;
    --encrypt)    ENCRYPT=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep 必须是非负整数"
command -v git >/dev/null 2>&1 || die "找不到 git"
git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 || die "$REPO_ROOT 不是 git 仓库"

if [[ $ENCRYPT -eq 1 ]]; then
  [[ -n "${AIEOS_BACKUP_PASSPHRASE:-}" ]] \
    || die "--encrypt 需要设置环境变量 AIEOS_BACKUP_PASSPHRASE"
  command -v openssl >/dev/null 2>&1 || die "找不到 openssl"
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

HOST_TAG="$(hostname -s 2>/dev/null | tr -c 'A-Za-z0-9._-' '-' || echo host)"
BASE="aieos-repo-$STAMP-$HOST_TAG"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aieos-repo-backup.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
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

# 加密：先写 .enc，确认体积正常后再删明文，任何一步失败都保留明文
seal() {
  local plain="$1"
  local enc="${plain}.enc"
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -pass env:AIEOS_BACKUP_PASSPHRASE \
    -in "$plain" -out "$enc"
  [[ -s "$enc" ]] || die "加密产出为空：$enc"
  rm -f "$plain"
  printf '%s' "$enc"
}

# ---------------------------------------------------------------- 1. git bundle
BUNDLE_PLAIN="$TMP_DIR/$BASE.bundle"
info "生成 git bundle（--all）…"
git -C "$REPO_ROOT" bundle create "$BUNDLE_PLAIN" --all >/dev/null 2>&1 \
  || die "git bundle 生成失败"
# verify 需要在一个真实仓库里执行，否则没有对象库可比对
git -C "$REPO_ROOT" bundle verify "$BUNDLE_PLAIN" >/dev/null 2>&1 \
  || die "git bundle 自检失败，已放弃本次备份"

HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
DIRTY=0
[[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]] && DIRTY=1

BUNDLE_OUT="$OUT_DIR/$BASE.bundle"
cp "$BUNDLE_PLAIN" "$BUNDLE_OUT"
chmod 600 "$BUNDLE_OUT"
BUNDLE_SHA="$(hash_file "$BUNDLE_OUT")"
if [[ $ENCRYPT -eq 1 ]]; then
  BUNDLE_OUT="$(seal "$BUNDLE_OUT")"
  BUNDLE_SHA="$(hash_file "$BUNDLE_OUT")"
fi
info "bundle：$(basename "$BUNDLE_OUT")（HEAD ${HEAD_SHA:0:8}，分支 $BRANCH）"

# ---------------------------------------------------------------- 2. 工作区快照
WORKTREE_OUT=""
WORKTREE_SHA=""
if [[ $MAKE_WORKTREE -eq 1 ]]; then
  command -v rsync >/dev/null 2>&1 || die "找不到 rsync"
  SNAP_DIR="$TMP_DIR/worktree"
  mkdir -p "$SNAP_DIR"
  info "生成工作区快照（含未提交改动，排除依赖与密钥）…"
  rsync -a \
    --exclude '.git/' \
    --exclude 'node_modules/' \
    --exclude '.next/' \
    --exclude '.venv/' \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'htmlcov/' \
    --exclude '.coverage*' \
    --exclude 'dist/' \
    --exclude 'build/' \
    --exclude 'data/' \
    --exclude 'logs/' \
    --exclude 'storage/' \
    --exclude '*.db' \
    --exclude '*.db-wal' \
    --exclude '*.db-shm' \
    --exclude '.env' \
    --exclude '.env.*' \
    --exclude '*.key' \
    --exclude '*.pem' \
    --exclude '*.p12' \
    --exclude '*credentials*' \
    --exclude '客户端交付_*/' \
    --exclude 'mobile/android/' \
    --exclude '.DS_Store' \
    --exclude 'Thumbs.db' \
    "$REPO_ROOT"/ "$SNAP_DIR"/

  # 双保险：快照里如果仍然出现密钥文件，直接中止，绝不上云
  LEAK="$(find "$SNAP_DIR" \( -name '.env' -o -name '.env.*' -o -name '*.key' \
            -o -name '*.pem' -o -name '*.p12' \) -not -name '.env.example' -print -quit)"
  [[ -z "$LEAK" ]] || die "快照中混入密钥文件：${LEAK#$SNAP_DIR/}，已中止"

  WORKTREE_OUT="$OUT_DIR/$BASE-worktree.tar.gz"
  tar -czf "$WORKTREE_OUT" -C "$SNAP_DIR" .
  chmod 600 "$WORKTREE_OUT"
  WORKTREE_SHA="$(hash_file "$WORKTREE_OUT")"
  if [[ $ENCRYPT -eq 1 ]]; then
    WORKTREE_OUT="$(seal "$WORKTREE_OUT")"
    WORKTREE_SHA="$(hash_file "$WORKTREE_OUT")"
  fi
  info "工作区快照：$(basename "$WORKTREE_OUT")（未提交改动：$([[ $DIRTY -eq 1 ]] && echo 有 || echo 无)）"
fi

# ---------------------------------------------------------------- 3. 数据备份
DATA_DIR=""
if [[ $WITH_DATA -eq 1 ]]; then
  DATA_DIR="$OUT_DIR/db-$STAMP"
  info "调用 tools/backup.sh 备份数据库与 .env…"
  "$SCRIPT_DIR/backup.sh" --out "$DATA_DIR" >/dev/null \
    || die "tools/backup.sh 失败"
  if [[ $ENCRYPT -eq 1 ]]; then
    # 只保留加密后的 tar.gz；明文目录里含 .env 与数据库，必须删掉
    shopt -s nullglob
    for f in "$DATA_DIR"/aieos-backup-*.tar.gz; do
      seal "$f" >/dev/null
    done
    for d in "$DATA_DIR"/aieos-backup-*/; do
      rm -rf -- "$d"
    done
    shopt -u nullglob
  fi
fi

# ---------------------------------------------------------------- 4. manifest
MANIFEST="$OUT_DIR/repo-manifest-$STAMP.json"
python3 - "$MANIFEST" "$HEAD_SHA" "$BRANCH" "$DIRTY" \
         "$BUNDLE_OUT" "$WORKTREE_OUT" "$DATA_DIR" "$REPO_ROOT" <<'PY'
import json, os, sys
from datetime import datetime, timezone

manifest, head, branch, dirty, bundle, worktree, data, repo = sys.argv[1:9]

def sha(path):
    if not path or not os.path.isfile(path):
        return None
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def entry(path):
    if not path or not os.path.exists(path):
        return None
    if os.path.isdir(path):
        files = sorted(os.listdir(path))
        total = sum(os.path.getsize(os.path.join(path, f))
                    for f in files if os.path.isfile(os.path.join(path, f)))
        return {"path": path, "kind": "dir", "files": files, "bytes": total}
    return {"path": path, "kind": "file", "bytes": os.path.getsize(path),
            "sha256": sha(path)}

doc = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "host": os.uname().nodename,
    "repo": repo,
    "head": head,
    "branch": branch,
    "worktree_dirty": bool(int(dirty)),
    "artifacts": [e for e in (entry(bundle), entry(worktree), entry(data)) if e],
    "restore": "见 docs/BACKUP_RUNBOOK.md 第 6 节",
}
with open(manifest, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)
print(manifest)
PY

# ---------------------------------------------------------------- 5. 保留策略
if [[ "$KEEP" -gt 0 ]]; then
  prune() {
    local pattern="$1"
    local old
    # shellcheck disable=SC2012
    old="$(ls -1t $pattern 2>/dev/null | tail -n +$((KEEP + 1)) || true)"
    [[ -z "$old" ]] || while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      rm -rf -- "$f"
      info "清理过期备份：$(basename "$f")"
    done <<< "$old"
  }
  shopt -s nullglob
  prune "$OUT_DIR/aieos-repo-*.bundle*"
  prune "$OUT_DIR/aieos-repo-*-worktree.tar.gz*"
  prune "$OUT_DIR/repo-manifest-*.json"
  shopt -u nullglob
fi

info "完成。输出目录：$OUT_DIR"
[[ $ENCRYPT -eq 1 ]] && info "产物已加密（AES-256，口令来自 AIEOS_BACKUP_PASSPHRASE，未写盘）。"
exit 0
