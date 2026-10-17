# AI-EOS 桌面客户端（V4.0-P1）

Electron 壳 + 便携后端（FastAPI/SQLite @8001）+ Web 前端（Next.js @8000），三平台通用。

## 架构

```
desktop/            ← 本目录：Electron 主进程（主进程拉起/复用便携服务）
apps/api            ← FastAPI 便携模式（sqlite+aiosqlite，data/aieos.db 单文件）
apps/web            ← Next.js 生产前端（.next，烘焙 NEXT_PUBLIC_API_URL=http://localhost:8001）
data/               ← SQLite 数据库 + 快照
storage/            ← 交付文件产出物
logs/desktop-*.log  ← 桌面壳/后端/前端日志
```

## 设计原则

- **不重写原生 UI**：整个既有 Web 前端原样装进 BrowserWindow（含登录/指挥台/专家库全部页面）。
- **前后端复用**：主进程先探测 8001/8000 端口——dev 场景直接复用已起的后端前端；双击便携场景才自拉起子进程。
- **数据单文件**：全部落 `data/aieos.db`，备份=拷目录；打包分发时 data/ 随包走。
- **API 端口烘焙一致性**：.next 构建时以 `NEXT_PUBLIC_API_URL=http://localhost:8001` 烘焙；若检测到现有 `.next` 烘焙的是别的端口，桌面壳启动时自动重建一次。

## 快速开始（开发机）

```bash
cd desktop
npm install
# 确保便携后端在 8001（或让它自动拉起）：
#   手动：cd apps/api && DATABASE_URL=sqlite+aiosqlite:///$PWD/../../data/aieos.db uv run uvicorn app.main:app --port 8001
# 然后：
npm start
```

## 出安装包

```bash
cd desktop
npm run dist:mac     # dmg + zip（x64/arm64）
npm run dist:win     # nsis 安装器 + portable 绿色版
npm run dist:linux   # AppImage + deb
```

> Windows 的 NSIS 安装器包含 `resources/app`，首次双击运行会自动安装 Python 依赖并构建前端（同 packaging/start-win.bat 逻辑的超集；若目标机无 python，可在安装器内嵌 `vendor/python`，后续 V4.2 签名时一起处理）。

## 与 V4.1 移动端的关系

桌面壳与未来的 Expo 移动 App 共用同一套 API（`/auth`、`/command`、`/experts` …）：
桌面=全功能；移动=老板视角（KPI/审批/一句话/服务连接），API 层已就绪。
