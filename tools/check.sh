#!/usr/bin/env bash
# AI-EOS 交付前自检门禁
#
# 一条命令跑完所有"出问题会很难查"的检查，任何一项失败即退出码非零。
# 目的不是追求形式上的绿色，而是把这次审计暴露出的三类缺陷
# （未定义变量 / 契约错配 / 失败静默）钉死在自动化里。
#
# 检查分两层：
#   零件级 —— 静态检查 / 依赖导入 / 契约 / 类型 / lint / 单元测试
#   链路级 —— 端到端主链路试跑（真实进程 + 真实 HTTP + 真实数据库，
#             模型侧用本地假服务，因此不需要任何真实密钥）
#
# 用法：
#   ./tools/check.sh            # 全部检查
#   ./tools/check.sh --fast     # 只做静态与前端检查，跳过测试与端到端试跑
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT/apps/api"
WEB="$ROOT/apps/web"
PY="$API/.venv/bin/python"

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

FAILED=0
step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }

if [[ ! -x "$PY" ]]; then
  echo "找不到 $PY —— 请先在 apps/api 下创建 .venv 并安装依赖" >&2
  exit 2
fi

# ---------------------------------------------------------------- 后端静态检查
step "后端：未定义名 / 语法错误（pyflakes）"
if OUT="$("$PY" -m pyflakes "$API/app" 2>&1 | grep -E 'undefined name|syntax error' || true)"; then
  if [[ -z "$OUT" ]]; then ok "无未定义名"; else bad "存在未定义名："; echo "$OUT"; fi
else
  bad "pyflakes 不可用（pip install pyflakes）"
fi

step "后端：应用可导入 + OpenAPI 可生成"
IMPORT_PROBE='
import app.main as m
print(len(m.app.openapi()["paths"]), "条路径")
'
if OUT="$(cd "$API" && "$PY" -c "$IMPORT_PROBE" 2>&1)"; then
  ok "导入成功（${OUT}）"
else
  bad "应用无法导入："; echo "$OUT"
fi

step "后端：SQLAlchemy mapper 可配置"
MAPPER_PROBE='
import app.models
from sqlalchemy.orm import configure_mappers
configure_mappers()
print("ok")
'
if OUT="$(cd "$API" && "$PY" -c "$MAPPER_PROBE" 2>&1)"; then
  ok "mapper 配置成功"
else
  bad "mapper 配置失败："; echo "$OUT"
fi

# ---------------------------------------------------------------- 契约
step "前后端接口契约（404 / 405 门禁）"
if OUT="$(cd "$API" && "$PY" "$ROOT/tools/check_api_contract.py" --strict 2>&1)"; then
  ok "契约一致"
  grep -E '动态拼接' <<<"$OUT" && true
else
  bad "存在接口契约不匹配："; echo "$OUT"
fi

# ---------------------------------------------------------------- 前端
step "前端：TypeScript 类型检查（strict）"
if [[ -x "$WEB/node_modules/.bin/tsc" ]]; then
  if OUT="$(cd "$WEB" && ./node_modules/.bin/tsc --noEmit 2>&1)"; then
    ok "0 个类型错误"
  else
    bad "存在类型错误："; echo "$OUT" | head -40
  fi
else
  printf '  \033[33m－\033[0m 跳过（未安装 node_modules）\n'
fi

step "前端：ESLint"
# lint 必须进门禁：改造前项目积累了 36 个 error（其中 32 处是"用三元表达式
# 做副作用"这种极易被误删的写法），而 next.config 里 ignoreDuringBuilds=true
# 把它们全部掩盖了。不设门禁，这类问题会再次堆积。
if [[ -x "$WEB/node_modules/.bin/eslint" ]]; then
  if OUT="$(cd "$WEB" && ./node_modules/.bin/eslint app components lib 2>&1)"; then
    ok "0 个问题"
  else
    bad "存在 lint 问题："; echo "$OUT" | tail -30
  fi
else
  printf '  \033[33m－\033[0m 跳过（未安装 node_modules）\n'
fi

# ---------------------------------------------------------------- 测试
if [[ $FAST -eq 0 ]]; then
  step "测试套件（离线、可重复）"
  if OUT="$(cd "$API" && "$PY" -m pytest 2>&1 | tail -3)"; then
    ok "$(echo "$OUT" | grep -E 'passed|failed' | tail -1)"
  else
    bad "测试未通过："; echo "$OUT"
  fi
fi

# ---------------------------------------------------------------- 端到端试跑
# 前面所有检查都是"零件级"的：它们能证明每个零件没问题，
# 但证明不了"整条链路能跑通"——而用户真正关心的是后者。
# 这一步用真实进程 + 真实 HTTP + 真实数据库把主链路跑一遍，
# 模型侧用本地假服务，因此不需要任何真实密钥，也能在 CI 里跑。
if [[ $FAST -eq 0 ]]; then
  step "端到端主链路试跑（真实进程 + 假模型服务，无需密钥）"
  SYS_PY="$(command -v python3 || true)"
  if [[ -z "$SYS_PY" ]]; then
    printf '  \033[33m－\033[0m 跳过（PATH 中找不到 python3）\n'
  elif OUT="$(cd "$ROOT" && "$SYS_PY" tools/e2e_smoke.py 2>&1 | tail -14)"; then
    ok "$(printf '%s\n' "$OUT" | grep -E '端到端试跑(通过|未通过)' | tail -1)"
  else
    bad "端到端试跑未通过："; printf '%s\n' "$OUT"
  fi
fi

# ---------------------------------------------------------------- 汇总
printf '\n'
if [[ $FAILED -eq 0 ]]; then
  printf '\033[32m全部检查通过。\033[0m\n'
else
  printf '\033[31m存在未通过的检查项，请勿直接交付。\033[0m\n'
fi
exit $FAILED
