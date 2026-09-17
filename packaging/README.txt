# AI-EOS 便携包 (Portable Edition)
# 双击即可在任何电脑上使用（要求：macOS/Linux 已有 python3 + node；Windows 同理）
# 数据持久化在 data/ 目录下（SQLite 单文件），产出物在 storage/ 下
#
# 文件清单：
#   packaging/start-mac.sh       macOS 启动器（双击或终端运行）
#   packaging/start-linux.sh     Linux 启动器（chmod +x 后双击或终端运行）
#   packaging/start-win.bat      Windows 启动器（双击）
#   apps/api/                    FastAPI 后端（SQLite 模式，DATABASE_URL 内联 config）
#   apps/web/                    Next.js 前端（生产产物 .next 目录）
#   data/                        运行时 SQLite 数据库
#   storage/                     文件产出物（PDF/Word/Excel 等）
#   logs/                        运行日志

# 使用说明（发给客户）：
# 1. 解压任意文件夹
# 2. 确保目标电脑有 python3 与 node（无则按 start-*.sh 提示一键安装）
# 3. 双击启动器（start-mac/start-win/start-linux）
# 4. 浏览器自动打开 http://localhost:8000
# 5. 数据永久保存在 data/ 文件夹（随包带走）
