@echo off
:: AI-EOS 便携包 — Windows 双击启动器（纯 batch）
:: 依赖：python + node（建议用 winget 安装：winget install Python.Python.3.12 OpenJS.NodeJS）
:: 架构：FastAPI (SQLite) + Next.js 生产前端
:: 数据：data\  SQLite 数据库 + storage\ 文件产出物（均在启动器所在目录）
setlocal
chcp 65001 >nul
set API_PORT=8001
set FRONT_PORT=8000
set WORK_DIR=%~dp0
cd /d "%WORK_DIR%"
set PYTHONUNBUFFERED=1
:: 数据库 URL：SQLAlchemy 异步 SQLite 前缀不可少（缺了建表会被跳过）
set "DB_PATH=%WORK_DIR%data\aieos.db"
set "DB_PATH=%DB_PATH:\=/%"
set "DATABASE_URL=sqlite+aiosqlite:///%DB_PATH%"
:: 密钥引导：JWT_SECRET 留空时后端会自动生成随机密钥并落盘到这里。
:: 显式指定路径，避免 uvicorn 的工作目录（apps\api）把密钥写进包外。
:: 备份 data\ 时请连同该文件一起备份：丢了 = 所有登录态失效。
set "AIEOS_JWT_SECRET_FILE=%WORK_DIR%data\jwt_secret.key"
set NEXT_PUBLIC_API_URL=http://localhost:%API_PORT%
set "API_LOG=%WORK_DIR%logs\api.log"
set "FRONT_LOG=%WORK_DIR%logs\front.log"

echo.
echo  [AI-EOS] 启动中...
echo  [AI-EOS] 后端端口: %API_PORT%
echo  [AI-EOS] 前端端口: %FRONT_PORT%
echo.

:: 检查 python / node
python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python 未找到。请先安装 Python 3.12
  pause
  exit /b 1
)
node --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js 未找到。请先安装 Node.js 18+
  pause
  exit /b 1
)
echo [OK] Python + Node 就绪

:: 创建目录
if not exist data mkdir data
if not exist storage mkdir storage
if not exist logs mkdir logs

:: 启动前快照 data\（只保留最近 5 份）
if exist data (
  set SNAP_TS=
  for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set SNAP_TS=%%t
)
if defined SNAP_TS (
  echo [BACKUP] 快照 data\ -^> data-backup-%SNAP_TS%
  xcopy data "data-backup-%SNAP_TS%\" /E /I /Q /Y >nul
  for /f "skip=5" %%d in ('dir /b /ad /o-n "data-backup-*" 2^>nul') do (
    echo [BACKUP] 清理旧快照: %%d
    rd /s /q "%%d"
  )
  set SNAP_TS=
)

:: 停止旧进程（仅这两个端口）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%API_PORT%" ^| findstr LISTENING') do taskkill /pid %%p /f >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%FRONT_PORT%" ^| findstr LISTENING') do taskkill /pid %%p /f >nul 2>&1

:: 首次运行自动装依赖：检测不到 fastapi 说明依赖从未安装
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
  echo [DEPS] 未检测到 Python 依赖，首次运行自动安装（约 1-3 分钟）...
  pushd apps\api
  where uv >nul 2>&1
  if not errorlevel 1 (
    echo [DEPS] 使用 uv 安装依赖...
    uv sync
  ) else (
    echo [DEPS] 使用 pip 安装依赖...
    pip install .
  )
  popd
  python -c "import fastapi" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请查看上方输出后重试
    pause
    exit /b 1
  )
  echo [OK] 依赖安装完成
)

:: 启动后端（必须以 apps\api 为工作目录，app.main 才能被 import；日志追加）
echo [START] 后端 %API_PORT% ...
pushd apps\api
start "ai-eos-api" /b cmd /c "python -m uvicorn app.main:app --host 0.0.0.0 --port %API_PORT% --log-level info ^>^> "%API_LOG%" 2^>^&1"
popd
:: 等待后端就绪（最多 40 秒）
set /a i=0
:WAIT_API
if %i% GEQ 40 (
  echo [ERROR] 后端启动超时，请查看 logs\api.log
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
set /a i+=1
curl -s --max-time 2 http://localhost:%API_PORT%/health >nul 2>&1
if not errorlevel 1 (
  echo [OK] 后端就绪
) else (
  goto :WAIT_API
)

:: 启动前端（若 .next 不存在则先 build；日志追加）
echo [START] 前端 %FRONT_PORT% ...
pushd apps\web
if not exist ".next" (
  echo [BUILD] 首次构建前端，请稍候...
  call npm install --prefer-offline
  call npx next build --no-lint
)
start "ai-eos-web" /b cmd /c "npx next start -p %FRONT_PORT% ^>^> "%FRONT_LOG%" 2^>^&1"
popd
:: 等待前端就绪
set /a i=0
:WAIT_FRONT
if %i% GEQ 20 (
  echo [ERROR] 前端启动超时，请查看 logs\front.log
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
set /a i+=1
curl -s --max-time 2 http://localhost:%FRONT_PORT%/ >nul 2>&1
if not errorlevel 1 (
  echo [OK] 前端就绪
) else (
  goto :WAIT_FRONT
)

echo.
echo ==========================================
echo   AI-EOS 已启动！
echo   打开浏览器: http://localhost:%FRONT_PORT%
echo   日志路径:   %WORK_DIR%logs\
echo ==========================================
start "" http://localhost:%FRONT_PORT%
echo.
echo [提示] 关闭窗口即可停止所有服务。
pause
