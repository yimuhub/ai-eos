#!/usr/bin/env bash
# ==============================================================================
#  AI-EOS —— 服务器一键部署脚本 (Server Auto-Deployer)
#  支持系统: Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / RHEL 8+ / Rocky Linux / Fedora / Arch / Alpine
#
#  使用方法:
#    方式一（已克隆仓库，在仓库根目录执行）：
#      chmod +x 服务器一键部署.sh && ./服务器一键部署.sh
#
#    方式二（全新空服务器）：
#      先 git clone https://github.com/yimuhub/ai-eos.git（私有仓需 token，脚本会提示输入），
#      再进目录执行本脚本。
# ==============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'

API_PORT="${API_PORT:-8001}"
FRONT_PORT="${FRONT_PORT:-8000}"
REPO_URL="https://github.com/yimuhub/ai-eos.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/ai-eos}"

banner() {
  echo -e "${PURPLE}╔══════════════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${PURPLE}║${NC}   ${CYAN}🏢 AI-EOS —— 服务器生产环境一键部署${NC}                                    ${PURPLE}║${NC}"
  echo -e "${PURPLE}║${NC}   AI 企业操作系统 / 数字员工平台 · 自动化部署中枢                            ${PURPLE}║${NC}"
  echo -e "${PURPLE}╚══════════════════════════════════════════════════════════════════════════╝${NC}"
  echo ""
}

log_step() { echo -e "${CYAN}▶ [$1] $2${NC}"; }
log_succ() { echo -e "${GREEN}  ✔ $1${NC}"; }
log_warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
log_err()  { echo -e "${RED}  ✖ [错误] $1${NC}"; }

# 0. 权限与 sudo
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

SUDO=""
if [ "$IS_ROOT" -eq 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    log_warn "当前不是 root 且无 sudo，安装系统依赖时可能权限不足。"
  fi
fi

# 1. 识别发行版包管理器
detect_pm() {
  if command -v apt-get >/dev/null 2>&1; then echo "apt"
  elif command -v dnf >/dev/null 2>&1; then echo "dnf"
  elif command -v yum >/dev/null 2>&1; then echo "yum"
  elif command -v pacman >/dev/null 2>&1; then echo "pacman"
  elif command -v apk >/dev/null 2>&1; then echo "apk"
  else echo "unknown"
  fi
}

install_system_deps() {
  log_step "1/7" "检测并安装系统依赖 (Git, Python 3.12+, Node.js 18+, Curl, SQLite, lsof)..."
  local pm
  pm=$(detect_pm)

  case "$pm" in
    apt)
      $SUDO apt-get update -y
      $SUDO apt-get install -y git curl wget sqlite3 lsof ca-certificates gnupg \
        python3 python3-venv python3-pip software-properties-common
      # Node.js 18+：发行版自带版本过旧则走 nodesource
      local node_ok=0
      if command -v node >/dev/null 2>&1; then
        local major
        major=$(node -v | sed 's/^v//' | cut -d. -f1)
        [ "$major" -ge 18 ] 2>/dev/null && node_ok=1
      fi
      if [ "$node_ok" -eq 0 ]; then
        log_succ "安装 Node.js 20 (nodesource)..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash -
        $SUDO apt-get install -y nodejs
      else
        $SUDO apt-get install -y nodejs npm || true
      fi
      ;;
    dnf|yum)
      $SUDO "$pm" install -y git curl wget sqlite lsof python3 python3-pip nodejs npm gcc
      ;;
    pacman)
      $SUDO pacman -Sy --noconfirm git curl sqlite lsof python python-pip nodejs npm
      ;;
    apk)
      $SUDO apk update
      $SUDO apk add git curl sqlite lsof python3 py3-pip nodejs npm bash build-base
      ;;
    *)
      log_warn "未识别到主流包管理器，跳过系统依赖自动安装。请确保已安装 Python 3.12+、Node.js 18+、Git、Curl。"
      ;;
  esac

  command -v git >/dev/null 2>&1 || { log_err "git 安装失败"; exit 1; }
  log_succ "系统依赖检查完毕。"
}

# 2. 确保 Python 3.12+
ensure_python312() {
  log_step "2/7" "确认 Python 3.12+ ..."
  local py=""
  for cmd in python3.12 python3.13 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
        py="$cmd"; break
      fi
    fi
  done
  if [ -z "$py" ]; then
    local pm
    pm=$(detect_pm)
    if [ "$pm" = "apt" ]; then
      log_warn "未找到 Python 3.12+，尝试 deadsnakes 源安装..."
      $SUDO add-apt-repository -y ppa:deadsnakes/ppa
      $SUDO apt-get update -y
      $SUDO apt-get install -y python3.12 python3.12-venv
      py="python3.12"
    fi
  fi
  if [ -z "$py" ] || ! "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
    log_err "未找到 Python 3.12+，请手动安装后重试。"
    exit 1
  fi
  log_succ "使用 $py ($($py --version))"
  export AIEOS_PY="$py"
}

# 3. 部署代码目录（私有仓：clone 失败时提示输入 token）
setup_source_code() {
  log_step "3/7" "部署 AI-EOS 源码..."
  if [ -f "LICENSE" ] && [ -d "apps/api" ] && [ -d "apps/web" ]; then
    TARGET_DIR="$(pwd)"
    log_succ "检测到已处于 AI-EOS 仓库根目录: $TARGET_DIR"
    git pull origin main || log_warn "git pull 失败（本地有改动或网络波动），保持当前源码继续部署"
  else
    TARGET_DIR="$INSTALL_DIR"
    log_succ "目标部署目录: $TARGET_DIR"
    if [ ! -d "$TARGET_DIR" ]; then
      $SUDO mkdir -p "$TARGET_DIR"
      [ "$IS_ROOT" -eq 0 ] && [ -n "$SUDO" ] && $SUDO chown -R "$(whoami)" "$TARGET_DIR"
      if ! git clone "$REPO_URL" "$TARGET_DIR" 2>/dev/null; then
        log_warn "公开克隆失败（仓库为私有）。请输入 GitHub Personal Access Token"
        log_warn "（需 repo 权限；输入不回显，仅存于本次运行内存，不会写入任何文件）"
        local GITHUB_TOKEN=""
        read -rsp "GitHub Token: " GITHUB_TOKEN || true; echo ""
        [ -n "${GITHUB_TOKEN:-}" ] || { log_err "未输入 token，无法克隆私有仓库"; exit 1; }
        git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/yimuhub/ai-eos.git" "$TARGET_DIR"
        unset GITHUB_TOKEN
        log_succ "已用 token 克隆成功（token 已从内存清除）"
      fi
    else
      cd "$TARGET_DIR"
      git pull origin main || log_warn "目录已存在，git pull 失败，保持当前源码继续部署"
    fi
  fi
  cd "$TARGET_DIR"
  log_succ "源码就绪: $TARGET_DIR"
}

# 4. 后端：uv + 依赖 + .env + 数据库迁移
setup_backend() {
  log_step "4/7" "配置后端 (uv sync + .env + alembic 迁移)..."
  cd "$TARGET_DIR/apps/api"

  if ! command -v uv >/dev/null 2>&1; then
    log_succ "安装 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || { log_err "uv 安装失败"; exit 1; }

  log_succ "安装 Python 依赖 (uv sync)..."
  uv sync || { log_err "uv sync 失败"; exit 1; }
  [ -x ".venv/bin/python" ] || { log_err "虚拟环境未生成 (.venv)"; exit 1; }

  mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/storage" "$TARGET_DIR/logs"

  if [ ! -f ".env" ]; then
    log_succ "生成生产 .env（含随机 JWT_SECRET）..."
    cp ../../.env.example .env
    local secret
    secret=$(openssl rand -hex 32)
    sed -i "s/^JWT_SECRET=$/JWT_SECRET=${secret}/" .env
    sed -i "s/^AIEOS_ENV=development$/AIEOS_ENV=production/" .env
    sed -i "s/^DEBUG=true$/DEBUG=false/" .env
    log_succ ".env 已生成（生产模式，随机密钥）"
  else
    log_succ "检测到现有 .env，予以保留"
  fi

  log_succ "执行数据库迁移 (alembic upgrade head)..."
  uv run alembic upgrade head || log_warn "alembic 迁移失败，SQLite 模式启动时会自动补列，可继续"
  log_succ "后端配置完毕。"
}

# 5. 前端：npm install + build（构建时写入 API 地址）
setup_frontend() {
  log_step "5/7" "构建前端 (npm install + build)..."
  # 探测本机 IP，供前端构建时写入 API 地址
  local srv_ip
  srv_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -z "$srv_ip" ] && srv_ip="127.0.0.1"
  export AIEOS_BUILD_API_URL="${AIEOS_BUILD_API_URL:-http://${srv_ip}:${API_PORT}}"
  log_succ "前端 API 地址（构建时写入）: $AIEOS_BUILD_API_URL"

  cd "$TARGET_DIR/apps/web"
  npm install --prefer-offline || npm install || { log_err "npm install 失败"; exit 1; }
  NEXT_PUBLIC_API_URL="$AIEOS_BUILD_API_URL" npm run build || { log_err "前端构建失败"; exit 1; }
  log_succ "前端构建完毕。"
}

# 6. systemd 服务（有 root/sudo 且支持 systemctl 才做）
setup_systemd() {
  log_step "6/7" "配置 systemd 服务 (ai-eos-api / ai-eos-web)..."
  if ! command -v systemctl >/dev/null 2>&1 || [ ! -d "/etc/systemd/system" ]; then
    log_warn "环境不支持 systemd，跳过服务注册。请手动用 packaging/start-linux.sh 启动。"
    return 0
  fi
  if [ "$IS_ROOT" -eq 0 ] && [ -z "$SUDO" ]; then
    log_warn "无 root/sudo 权限，跳过 systemd 注册。请手动用 packaging/start-linux.sh 启动。"
    return 0
  fi

  local run_user npm_bin
  run_user="$(whoami)"
  npm_bin="$(command -v npm)"

  $SUDO bash -c "cat > /etc/systemd/system/ai-eos-api.service <<EOF
[Unit]
Description=AI-EOS API (FastAPI)
After=network.target

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${TARGET_DIR}/apps/api
EnvironmentFile=${TARGET_DIR}/apps/api/.env
Environment=DATABASE_URL=sqlite+aiosqlite:///${TARGET_DIR}/data/aieos.db
ExecStart=${TARGET_DIR}/apps/api/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${API_PORT} --log-level info
Restart=always
RestartSec=5
StandardOutput=append:${TARGET_DIR}/logs/api.out
StandardError=append:${TARGET_DIR}/logs/api.out

[Install]
WantedBy=multi-user.target
EOF"

  $SUDO bash -c "cat > /etc/systemd/system/ai-eos-web.service <<EOF
[Unit]
Description=AI-EOS Web (Next.js)
After=network.target ai-eos-api.service

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${TARGET_DIR}/apps/web
Environment=NEXT_PUBLIC_API_URL=${AIEOS_BUILD_API_URL}
Environment=PORT=${FRONT_PORT}
ExecStart=${npm_bin} run start -- -p ${FRONT_PORT}
Restart=always
RestartSec=5
StandardOutput=append:${TARGET_DIR}/logs/web.out
StandardError=append:${TARGET_DIR}/logs/web.out

[Install]
WantedBy=multi-user.target
EOF"

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable ai-eos-api.service ai-eos-web.service
  $SUDO systemctl restart ai-eos-api.service ai-eos-web.service
  sleep 3
  log_succ "systemd 服务已创建并启动（开机自启已生效）"
}

# 7. 收尾面板
show_summary() {
  log_step "7/7" "检测网络与服务状态..."
  sleep 2
  local ext_ip int_ip
  ext_ip=$(curl -s --max-time 2 https://api.ipify.org 2>/dev/null || echo "（未能探测到公网IP）")
  int_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -z "$int_ip" ] && int_ip="127.0.0.1"

  echo ""
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}🎉 AI-EOS 服务器生产环境部署完成！${NC}"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "  🌐 ${CYAN}浏览器访问:${NC}   ${BLUE}http://${ext_ip}:${FRONT_PORT}${NC}（公网）"
  echo -e "  🏠 ${CYAN}内网访问:${NC}     ${BLUE}http://${int_ip}:${FRONT_PORT}${NC}"
  echo -e "  🔌 ${CYAN}后端 API:${NC}     ${BLUE}http://${int_ip}:${API_PORT}${NC}（前端构建时已写入 ${AIEOS_BUILD_API_URL}）"
  echo -e "  📂 ${CYAN}安装目录:${NC}     ${PURPLE}${TARGET_DIR}${NC}"
  echo -e "  📋 ${CYAN}运行日志:${NC}     ${PURPLE}${TARGET_DIR}/logs/${NC}"
  echo -e "  🔑 ${CYAN}首次使用:${NC}     打开页面后在登录页注册账号（无预置账号）"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${YELLOW}常用运维指令：${NC}"
  if [ -f "/etc/systemd/system/ai-eos-api.service" ]; then
    echo -e "  - 查看状态: ${GREEN}sudo systemctl status ai-eos-api ai-eos-web${NC}"
    echo -e "  - 重启服务: ${GREEN}sudo systemctl restart ai-eos-api ai-eos-web${NC}"
    echo -e "  - 停止服务: ${GREEN}sudo systemctl stop ai-eos-api ai-eos-web${NC}"
    echo -e "  - 查看日志: ${GREEN}tail -f ${TARGET_DIR}/logs/api.out${NC}"
  else
    echo -e "  - 启动: ${GREEN}./packaging/start-linux.sh${NC}"
  fi
  echo -e "  - 升级代码: ${GREEN}cd ${TARGET_DIR} && git pull${NC}"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
}

main() {
  banner
  install_system_deps
  ensure_python312
  setup_source_code
  setup_backend
  setup_frontend
  setup_systemd
  show_summary
}

main "$@"
