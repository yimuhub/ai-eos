/**
 * AI-EOS 桌面客户端主进程
 * 架构：Electron 壳 + 便携后端(FastAPI/SQLite @8001) + Web 前端(Next.js @8000)
 *
 * 设计原则（延续 V3/V4 理念）：不重写任何原生 UI，直接把既有 Web 前端
 * 装进原生窗口；后端用 SQLite 单文件（data/ 目录），三平台通用。
 *
 * 平台感知：
 *  - macOS / Linux：python3 + node（系统自带或便携打包）
 *  - Windows：py/python + node
 * 启动时复用已监听端口的服务（dev 场景），否则自拉起（便携双击场景）。
 */
const { app, BrowserWindow, Menu, shell, dialog, ipcMain } = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");

const isWin = process.platform === "win32";
const isMac = process.platform === "darwin";
// desktop/ 在仓库根目录下，根目录 = 上级
const ROOT = path.resolve(__dirname, "..");
const DATA_DIR = path.join(ROOT, "data");
const LOG_DIR = path.join(ROOT, "logs");
const API_PORT = 8001; // 便携后端（SQLite）
const FRONT_PORT = 8000; // Web 前端

const API_BASE = `http://localhost:${API_PORT}`;
const FRONT_URL = `http://localhost:${FRONT_PORT}`;

let apiProc = null;
let frontProc = null;
let mainWindow = null;
let quitting = false;
let _shuttingDown = false;

function log(...a) {
  const line = `[${new Date().toISOString().slice(11, 19)}] [AI-EOS Desktop] ${a.join(" ")}\n`;
  process.stdout.write(line);
}

function ensureDirs() {
  for (const d of [DATA_DIR, LOG_DIR]) {
    try { fs.mkdirSync(d, { recursive: true }); } catch {}
  }
}

// ---------- HTTP 健康探测（避免引入依赖） ----------
function probe(url, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 400);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(false); });
  });
}

// ---------- 端口是否已被监听（dev 场景复用） ----------
function portInUse(port) {
  const net = require("net");
  return new Promise((resolve) => {
    const s = net.createServer();
    s.once("error", () => resolve(true));
    s.once("listening", () => { s.close(); resolve(false); });
    s.listen(port, "127.0.0.1");
  });
}

function waitReady(url, tries, stepMs) {
  return new Promise((resolve) => {
    let i = 0;
    const tick = async () => {
      i++;
      if (await probe(url)) return resolve(true);
      if (i >= tries) return resolve(false);
      setTimeout(tick, stepMs);
    };
    tick();
  });
}

// ---------- 启动便携后端（FastAPI + SQLite） ----------
function startApi() {
  const py = isWin ? "py" : "python3";
  const logFile = fs.openSync(path.join(LOG_DIR, "desktop-api.log"), "a");
  const env = {
    ...process.env,
    DATABASE_URL: `sqlite+aiosqlite:///${path.join(DATA_DIR, "aieos.db")}`,
    PYTHONUNBUFFERED: "1",
    PYTHONPATH: path.join(ROOT, "apps/api"),
  };
  // 用 uv 若存在（与开发机一致），否则回退 python -m uvicorn
  const useUv = fs.existsSync(path.join(ROOT, ".venv")) || process.env.UV === "1";
  const args = useUv
    ? ["run", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT), "--log-level", "info"]
    : ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT), "--log-level", "info"];
  apiProc = spawn(useUv ? "uv" : py, args, {
    cwd: path.join(ROOT, "apps/api"),
    env,
    stdio: ["ignore", logFile, logFile],
    detached: !isMac,
  });
  log(`后端 uvicorn 已拉起 (PID ${apiProc.pid}) → ${API_BASE}`);
  return apiProc;
}

// ---------- 启动 Web 前端（Next.js 生产模式） ----------
async function startFront() {
  const webDir = path.join(ROOT, "apps/web");
  const nextDir = path.join(webDir, ".next");
  // 烘焙一致性：.next 必须是用 8001 构建的，否则前端会指向 8000 拿不到便携后端
  const stampFile = path.join(nextDir, ".desktop-api-port");
  const needBuild = !fs.existsSync(nextDir) || fs.existsSync(stampFile)
    ? fs.readFileSync(stampFile, "utf8").trim() !== String(API_PORT)
    : true;
  if (needBuild) {
    log("前端 .next 未就绪或 API 端口不匹配，执行一次性构建（首次约 1-2 分钟）...");
    log(`→ ${nextDir}`);
    const logFile = fs.openSync(path.join(LOG_DIR, "desktop-build.log"), "a");
    const build = spawn("npm", ["run", "build"], {
      cwd: webDir,
      env: { ...process.env, NEXT_PUBLIC_API_URL: API_BASE },
      stdio: ["ignore", logFile, logFile],
    });
    await new Promise((res) => build.on("close", (code) => res(code === 0)));
    if (fs.existsSync(stampFile) === false && fs.existsSync(nextDir)) {
      try { fs.writeFileSync(stampFile, String(API_PORT)); } catch {}
    }
  }
  const logFile = fs.openSync(path.join(LOG_DIR, "desktop-front.log"), "a");
  frontProc = spawn("npm", ["run", "start"], {
    cwd: webDir,
    env: { ...process.env, PORT: String(FRONT_PORT), NEXT_PUBLIC_API_URL: API_BASE },
    stdio: ["ignore", logFile, logFile],
    detached: !isMac,
  });
  log(`前端 next 已拉起 (PID ${frontProc.pid}) → ${FRONT_URL}`);
  return frontProc;
}

// ---------- 启动主窗口 ----------
function createWindow() {
  const w = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    backgroundColor: "#0b0e14",
    show: false,
    title: "AI-EOS",
    icon: isWin ? path.join(__dirname, "icon.ico") : undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  // 外链走系统浏览器，不新开窗口
  w.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) shell.openExternal(url);
    return { action: "deny" };
  });

  w.once("ready-to-show", () => { w.show(); });
  w.loadURL(FRONT_URL);
  mainWindow = w;
}

// ---------- 菜单（极简：查看 + 关于） ----------
function buildMenu() {
  const view = {
    label: "查看",
    submenu: [
      { role: "reload" },
      { role: "toggleDevTools" },
      { type: "separator" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      { role: "resetZoom" },
      { type: "separator" },
      { role: "togglefullscreen" },
    ],
  };
  const about = {
    label: "关于 AI-EOS",
    click: () => dialog.showMessageBox(mainWindow, {
      title: "关于",
      message: "AI-EOS · 一句话指挥版 V4.0",
      detail: "人工智能企业操作系统桌面客户端。\n后端：FastAPI/SQLite · 前端：Next.js",
    }),
  };
  const tpl = [
    ...(isMac ? [{ role: "appMenu" }] : []),
    { role: "fileMenu" },
    view,
    { role: "windowMenu" },
    { type: "separator" },
    about,
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(tpl));
}

// ---------- IPC：向前端暴露环境信息 ----------
function registerIpc() {
  ipcMain.handle("desktop:env", () => ({
    platform: process.platform,
    api: API_BASE,
    front: FRONT_URL,
    dataDir: DATA_DIR,
    version: app.getVersion(),
  }));
  ipcMain.handle("desktop:openDataDir", async () => {
    await shell.openPath(DATA_DIR);
  });
}

// ---------- 清理子进程 ----------
function killProc(p) {
  if (!p || p.killed) return;
  try {
    // 杀整个进程组（detached 场景），兜底直接 kill
    if (p.pid) {
      try { process.kill(-p.pid, "SIGTERM"); } catch { p.kill("SIGTERM"); }
      setTimeout(() => { try { process.kill(-p.pid, "SIGKILL"); } catch {} }, 3000);
    }
    p.killed = true;
  } catch {}
}

// 冒烟开关：AI_EOS_DESKTOP_SMOKE=1 时跳过拉起前后端，直接开窗口加载 about:blank，
// 5 秒后自动退出——用于无 GUI/无网络依赖验证主进程 whenReady 流程、菜单、IPC 注册不崩。
const SMOKE = process.env.AI_EOS_DESKTOP_SMOKE === "1";

app.whenReady().then(async () => {
  ensureDirs();
  buildMenu();
  registerIpc();

  if (SMOKE) {
    createWindow();
    mainWindow.loadURL("about:blank");
    log("[SMOKE] 主进程流程 OK（menu+ipc+window 创建无异常），5s 后自动退出");
    setTimeout(() => { killProc(apiProc); killProc(frontProc); app.quit(); }, 5000);
    return;
  }

  // 1. 复用已在监听的后端，否则自拉起
  if (await portInUse(API_PORT)) {
    log(`检测到 ${API_PORT} 已有后端，复用（dev 模式）`);
  } else {
    startApi();
    const ok = await waitReady(`${API_BASE}/health`, 60, 2000);
    if (!ok) {
      dialog.showErrorBox("AI-EOS 启动失败", `后端 ${API_BASE} 未就绪。\n请查看 logs/desktop-api.log`);
      app.quit();
      return;
    }
    log("后端就绪");
  }

  // 2. 复用已在监听的前端，否则自拉起
  if (await portInUse(FRONT_PORT)) {
    log(`检测到 ${FRONT_PORT} 已有前端，复用`);
  } else {
    await startFront();
    const ok = await waitReady(FRONT_URL, 90, 2000);
    if (!ok) {
      dialog.showErrorBox("AI-EOS 启动失败", `前端 ${FRONT_URL} 未就绪。\n请查看 logs/desktop-front.log`);
      app.quit();
      return;
    }
    log("前端就绪");
  }

  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", () => { quitting = true; });
app.on("will-quit", (e) => {
  if (!quitting || _shuttingDown) return;
  _shuttingDown = true; // 只拦一次，避免递归死循环（preventDefault 后再次 quit 会死锁）
  e.preventDefault();
  killProc(apiProc);
  killProc(frontProc);
  setTimeout(() => process.exit(0), 500); // 给子进程收尾，然后强制退出
});
process.on("SIGINT", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
process.on("SIGTERM", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
