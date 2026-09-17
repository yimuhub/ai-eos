#!/bin/bash
# AI-EOS 便携包 — Windows 双击启动器 (.bat)
# 依赖系统环境：python3 + node（建议预装：brew install python node 或用 winget install Python.Nodejs）
# 架构：FastAPI (SQLite) 单进程 + Next.js 生产前端（同端口 8000，FastAPI 托管静态文件）
# 数据：data/  SQLite 数据库 + storage/ 文件产出物

@echo off
chcp 65001 >nul
set API_PORT=8001
set FRONT_PORT=8000
set WORK_DIR=%~dp0
set PYTHONUNBUFFERED=1
set DATABASE_URL=%WORK_DIR%\data\aieos.db
set NEXT_PUBLIC_API_URL=http://localhost:%API_PORT%

echo.
echo  [AI-EOS] 启动中...
echo  [AI-EOS] 后端端口: %API_PORT%
echo  [AI-EOS] 前端端口: %FRONT_PORT%
echo.

:: 检查 python / node
python --version >nul 2>&1 || (
  echo [ERROR] python 未找到。请先安装 Python 3.9+
  pause
  exit /b 1
)
node --version >nul 2>&1 || (
  echo [ERROR] Node.js 未找到。请先安装 Node.js 18+
  pause
  exit /b 1
)
echo [OK] Python + Node 就绪

:: 停止旧进程
for /f "tokens=*" %%i in ('netstat -ano ^| findstr :%API_PORT%' ^| findstr LISTENING') do (
  taskkill /pid %%~ni /f >nul 2>&1
)
for /f "tokens=*" %%i in ('netstat -ano ^| findstr :%FRONT_PORT%' ^| findstr LISTENING') do (
  taskkill /pid %%~ni /f >nul 2>&1
)

:: 创建目录
if not exist data mkdir data
if not exist storage mkdir storage
if not exist logs mkdir logs

:: 启动后端
echo [START] 后端 %API_PORT% ...
start /b cmd /c "python -m uvicorn app.main:app --host 0.0.0.0 --port %API_PORT% --log-level info ^> logs\api.log 2>&1"
:: 等待后端就绪（最多 40 秒）
set /a i=0
:WAIT_API
if %i% GEQ 40 (echo [ERROR] 后端启动超时；查看 logs\api.log& pause& exit /b 1)
timeout /t 1 /nobreak >nul
set /a i+=1
curl -s --max-time 2 http://localhost:%API_PORT%/health >nul 2>&1 && echo [OK] 后端就绪 || goto :WAIT_API

:: 启动前端（若 .next 不存在则先 build）
echo [START] 前端 %FRONT_PORT% ...
pushd apps\web
if not exist ".next" (
  echo [BUILD] 首次构建前端，请稍候...
  npm install --prefer-offline 2^>nul
  npx next build --no-lint 2^>nul
)
start /b cmd /c "npx next start -p %FRONT_PORT% ^>..\logs\front.log 2>&1"
popd
:: 等待前端就绪
set /a i=0
:WAIT_FRONT
if %i% GEQ 20 (echo [ERROR] 前端启动超时；查看 logs\front.log& pause& exit /b 1)
timeout /t 1 /nobreak >nul
set /a i+=1
curl -s --max-time 2 http://localhost:%FRONT_PORT%/ >nul 2>&1 && echo [OK] 前端就绪 || goto :WAIT_FRONT

echo.
echo ==========================================
echo   AI-EOS 已启动！
echo   打开浏览器: http://localhost:%FRONT_PORT%
echo   日志路径:   %WORK_DIR%\logs\
echo ==========================================
start "" http://localhost:%FRONT_PORT%
echo.
echo [提示] 关闭窗口即可停止所有服务。
pause
