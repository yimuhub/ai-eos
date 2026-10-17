# AI-EOS 便携包 (Portable Edition)
# 数据持久化在 data/ 目录下（SQLite 单文件），产出物在 storage/ 下
# 每次启动前自动快照 data/（data-backup-时间戳，只保留最近 5 份）
#
# 文件清单：
#   packaging/start-mac.sh       macOS 启动器（双击或终端运行）
#   packaging/start-linux.sh     Linux 启动器（chmod +x 后双击或终端运行）
#   packaging/start-win.bat      Windows 启动器（双击）
#   apps/api/                    FastAPI 后端（SQLite 模式，DATABASE_URL 内联 config）
#   apps/web/                    Next.js 前端（生产产物 .next 目录）
#   data/                        运行时 SQLite 数据库
#   storage/                     文件产出物（PDF/Word/Excel 等）
#   logs/                        运行日志（追加写入，不再截断）

# 使用说明（发给客户）：
# 1. 解压到任意文件夹
# 2. 第一步：装依赖
#    - 目标电脑需有 python3 与 node（无则按 start-*.sh/.bat 的提示安装）
#    - 首次运行启动器会自动安装 Python 依赖（有 uv 用 uv sync，无 uv 用 pip，约 1-3 分钟）
#    - 前端首次启动会自动 npm install + 构建（约 1-2 分钟）
# 3. 第二步：启动
#    - 双击启动器（start-mac/start-win/start-linux），等待后端/前端就绪
# 4. 浏览器自动打开 http://localhost:8000
# 5. 首次使用请在登录页注册账号（项目不预置任何账号）
# 6. 数据永久保存在 data/ 文件夹（随包带走）
