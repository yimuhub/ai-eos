/**
 * AI-EOS 桌面客户端主进程（V5.1）
 * 架构：Electron 壳 + 便携后端(FastAPI/SQLite @8001) + Web 前端(Next.js @8000)
 *
 * 平台感知：
 *  - 开发场景（仓库内运行）：启动时复用已监听端口的服务，否则自拉起
 *  - 打包安装场景（dmg/exe/AppImage 装进机器）：app 内没有仓库文件，
 *    不拉起服务——直接探测服务器；探测不到时显示「服务未检测到」引导页，
 *    老板可重试 / 改服务器地址 / 打开数据目录
 *
 * 服务器地址持久化在 userData/settings.json（可指向本机便携 8000/8001，
 * 也可指向远程自托管服务器 http://192.168.x.x:8000 等）。
 */
const {
  app, BrowserWindow, Menu, shell, dialog, ipcMain, globalShortcut, screen,
} = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");

const isWin = process.platform === "win32";
const isMac = process.platform === "darwin";
// desktop/ 在仓库根目录下，根目录 = 上级（打包后 asar 内不存在 → REPO_PRESENT=false）
const ROOT = path.resolve(__dirname, "..");
const REPO_PRESENT = fs.existsSync(path.join(ROOT, "apps", "api")) && fs.existsSync(path.join(ROOT, "apps", "web"));
const DATA_DIR = path.join(ROOT, "data");
const LOG_DIR = path.join(ROOT, "logs");
const DEFAULT_API_PORT = 8001;   // 便携后端（SQLite）
const DEFAULT_FRONT_PORT = 8000; // Web 前端

// 常驻 CEO 小人的窗口尺寸：只留出小人 + 名字 + 悬停气泡，尽量少挡桌面
const PET_W = 180;
const PET_H = 232;
const DEFAULT_CEO_COMBO = isMac ? "meta+shift+k" : "ctrl+shift+k";

let apiProc = null;
let frontProc = null;
let mainWindow = null;
let petWindow = null;
let petEligible = false;   // 网页确认「已任命 CEO」后才显示，避免空窗
let petDragTimer = null;
let petDragOffset = null;
let ceoAccelerator = "";   // 当前成功注册的系统级组合键
let quitting = false;
let _shuttingDown = false;
let _updating = false;

// ---------- 服务器设置（userData/settings.json） ----------
function settingsFile() { return path.join(app.getPath("userData"), "settings.json"); }
function loadSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), "utf8")); } catch { return {}; }
}
function saveSettings(patch) {
  const s = { ...loadSettings(), ...patch };
  try {
    fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
    fs.writeFileSync(settingsFile(), JSON.stringify(s, null, 2));
  } catch (e) { log("settings 写入失败", e && e.message); }
  return s;
}
function currentSettings() {
  const s = loadSettings();
  return {
    frontUrl: s.frontUrl || `http://localhost:${s.frontPort || DEFAULT_FRONT_PORT}`,
    apiUrl: s.apiUrl || `http://localhost:${s.apiPort || DEFAULT_API_PORT}`,
  };
}

// 主进程日志：stdout（终端可见）+ userData/logs/main.log（打包后用户可见）
let _logFilePath = null;
function log(...a) {
  const line = `[${new Date().toISOString().slice(11, 19)}] [AI-EOS Desktop] ${a.join(" ")}\n`;
  process.stdout.write(line);
  try {
    if (!_logFilePath) {
      const dir = path.join(app.getPath("userData"), "logs");
      fs.mkdirSync(dir, { recursive: true });
      _logFilePath = path.join(dir, "main.log");
    }
    fs.appendFileSync(_logFilePath, line);
  } catch (e) {
    console.error("[AI-EOS Desktop] 主进程日志写入失败:", e && e.message);
  }
}

function ensureDirs() {
  for (const d of [DATA_DIR, LOG_DIR]) {
    try { fs.mkdirSync(d, { recursive: true }); } catch {}
  }
}

// ---------- HTTP 健康探测（避免引入依赖） ----------
function probe(url, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const req = http.get(url, (res) => {
      res.resume();
      done(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on("error", () => done(false));
    req.setTimeout(timeoutMs, () => { req.destroy(); done(false); });
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

// ---------- JWT 密钥（桌面版不落盘到仓库 data/，改由壳自己保管并经 env 注入） ----------
// 后端在 JWT_SECRET 为空/默认时会自己生成并写文件，但打包进 .app 后仓库目录
// 可能只读（asar/Program Files），所以这里显式生成一个放在 userData（必定可写），
// 用 env 传给子进程——桌面版的签名密钥从始至终不为空。
function jwtSecretFile() { return path.join(app.getPath("userData"), "jwt-secret.key"); }
let _jwtSecretCache = "";
function ensureJwtSecret() {
  if (_jwtSecretCache) return _jwtSecretCache;
  const f = jwtSecretFile();
  try {
    const existing = fs.readFileSync(f, "utf8").trim();
    // 历史遗留的公开占位值一律视为无效，重新生成
    if (existing && existing !== "aieos-portable-secret") {
      _jwtSecretCache = existing;
      return existing;
    }
  } catch { /* 首次运行：没有文件 */ }
  const secret = require("crypto").randomBytes(32).toString("hex");
  try {
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, secret, { mode: 0o600 });
    log(`已生成随机 JWT_SECRET 并保存到 ${f}；请备份该文件，丢失会导致所有登录态失效`);
  } catch (e) {
    log(`JWT_SECRET 文件写入失败（${e && e.message}），本次仅用内存密钥：重启后需重新登录`);
  }
  _jwtSecretCache = secret;
  return secret;
}

// ---------- 监听地址：默认只绑回环 ----------
// 0.0.0.0 + 匿名开放的注册接口 = 同一局域网里任何人都能建账号、撞库、
// 拿到 /companies 与 /agents 数据。桌面版是单机产品，默认只服务本机；
// 只有用户在「局域网共享」页显式打开开关（并重启后端）才对外监听。
function apiBindHost() {
  return loadSettings().lanShareEnabled === true ? "0.0.0.0" : "127.0.0.1";
}

// ---------- 启动便携后端（FastAPI + SQLite，仅仓库场景） ----------
function startApi() {
  const py = isWin ? "py" : "python3";
  const logFile = fs.openSync(path.join(LOG_DIR, "desktop-api.log"), "a");
  const useUv = fs.existsSync(path.join(ROOT, ".venv")) || process.env.UV === "1";
  const host = apiBindHost();
  const env = {
    ...process.env,
    DATABASE_URL: `sqlite+aiosqlite:///${path.join(DATA_DIR, "aieos.db")}`,
    PYTHONUNBUFFERED: "1",
    PYTHONPATH: path.join(ROOT, "apps/api"),
    AIEOS_HTTP_PORT: String(DEFAULT_API_PORT),  // mDNS 广播用（零依赖降级，缺失不影响启动）
    // 密钥由壳生成并显式注入：空密钥/默认密钥签出的 token 能通过鉴权，
    // 这条路径不能再依赖后端"自己生成"（打包后仓库目录可能只读）
    JWT_SECRET: process.env.JWT_SECRET || ensureJwtSecret(),
    AIEOS_JWT_SECRET_FILE: jwtSecretFile(),
    // 只监听 127.0.0.1 时不再向局域网广播：否则设备发现到的地址根本连不上，
    // 等于对外宣告了一个不存在的服务（也平白多一份多播流量）
    AI_EOS_MDNS: host === "0.0.0.0" ? "1" : "0",
  };
  const args = useUv
    ? ["run", "-m", "uvicorn", "app.main:app", "--host", host, "--port", String(DEFAULT_API_PORT), "--log-level", "info"]
    : ["-m", "uvicorn", "app.main:app", "--host", host, "--port", String(DEFAULT_API_PORT), "--log-level", "info"];
  apiProc = spawn(useUv ? "uv" : py, args, {
    cwd: path.join(ROOT, "apps/api"),
    env,
    stdio: ["ignore", logFile, logFile],
    detached: !isMac,
  });
  const thisProc = apiProc;
  // 缺 Python / uv 时 spawn 会异步抛 error（无监听 = 主进程未捕获异常直接崩）
  apiProc.on("error", (e) => {
    const msg = String(e && e.message || e);
    if (apiProc === thisProc) apiProc = null;
    log(`后端进程启动失败（Python/uv 未安装或不可执行）：${msg}`);
    showGuide(`后端启动失败：${msg}`);
  });
  log(`后端 uvicorn 已拉起 (PID ${apiProc.pid}) → http://localhost:${DEFAULT_API_PORT}（监听 ${host}${host === "127.0.0.1" ? "，仅本机可达" : "，局域网共享已开启"}）`);
  return apiProc;
}

// ---------- 启动 Web 前端（Next.js 生产模式，仅仓库场景） ----------
async function startFront() {
  const webDir = path.join(ROOT, "apps/web");
  const nextDir = path.join(webDir, ".next");
  const API_BASE = `http://localhost:${DEFAULT_API_PORT}`;
  // 烘焙一致性：.next 必须是用 8001 构建的，否则前端会指向 8000 拿不到便携后端
  const stampFile = path.join(nextDir, ".desktop-api-port");
  let portStamp = "";
  try { portStamp = fs.readFileSync(stampFile, "utf8").trim(); } catch {}
  const needBuild = !fs.existsSync(nextDir) || portStamp !== String(DEFAULT_API_PORT);
  if (needBuild) {
    log("前端 .next 未就绪或 API 端口不匹配，执行一次性构建（首次约 1-2 分钟）...");
    const logFile = fs.openSync(path.join(LOG_DIR, "desktop-build.log"), "a");
    const build = spawn("npm", ["run", "build"], {
      cwd: webDir,
      env: { ...process.env, NEXT_PUBLIC_API_URL: API_BASE },
      stdio: ["ignore", logFile, logFile],
    });
    await new Promise((res) => {
      build.on("close", (code) => res(code === 0));
      // 缺 npm / 无法执行时 'close' 不一定触发，必须在这里收口，否则启动流程永久挂起
      build.on("error", (e) => {
        log(`前端构建进程启动失败（npm 不可用？）：${e && e.message}`);
        res(false);
      });
    });
    if (fs.existsSync(nextDir)) {
      try { fs.writeFileSync(stampFile, String(DEFAULT_API_PORT)); } catch {}
    }
  }
  const logFile = fs.openSync(path.join(LOG_DIR, "desktop-front.log"), "a");
  frontProc = spawn("npm", ["run", "start"], {
    cwd: webDir,
    env: { ...process.env, PORT: String(DEFAULT_FRONT_PORT), NEXT_PUBLIC_API_URL: API_BASE },
    stdio: ["ignore", logFile, logFile],
    detached: !isMac,
  });
  const thisProc = frontProc;
  frontProc.on("error", (e) => {
    const msg = String(e && e.message || e);
    if (frontProc === thisProc) frontProc = null;
    log(`前端进程启动失败（npm 未安装或不可执行）：${msg}`);
    showGuide(`前端启动失败：${msg}`);
  });
  log(`前端 next 已拉起 (PID ${frontProc.pid}) → http://localhost:${DEFAULT_FRONT_PORT}`);
  return frontProc;
}

// ---------- 服务未检测到 → 引导页（玻璃拟态风格） ----------
// frontUrl/apiUrl 来自用户可编辑的 settings.json，插入 HTML 前必须转义
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function guideHtml() {
  const { frontUrl, apiUrl } = currentSettings();
  const safeFrontUrl = escapeHtml(frontUrl);
  const safeApiUrl = escapeHtml(apiUrl);
  const repo = REPO_PRESENT;
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    :root{--bg:#0B0E14;--fg:#F5F8FF;--mut:#8B93A7;--card:rgba(255,255,255,.055);
      --brd:rgba(255,255,255,.10);--accent:#6366F1;--accent2:#22D3EE;}
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%}
    body{background:radial-gradient(1200px 700px at 20% -10%,rgba(99,102,241,.20),transparent 60%),
      radial-gradient(900px 600px at 110% 110%,rgba(34,211,238,.14),transparent 60%),var(--bg);
      color:var(--fg);font:15px/1.6 -apple-system,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;
      display:flex;align-items:center;justify-content:center}
    .card{width:min(560px,92vw);background:var(--card);border:1px solid var(--brd);border-radius:20px;
      padding:40px 36px;backdrop-filter:blur(14px);animation:fu .45s ease both}
    @keyframes fu{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
    .dot{width:12px;height:12px;border-radius:50%;background:#F97316;display:inline-block;
      box-shadow:0 0 0 6px rgba(249,115,22,.15);animation:pulse 1.6s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 4px rgba(249,115,22,.22)}50%{opacity:.55;box-shadow:0 0 0 10px rgba(249,115,22,.05)}}
    h1{font-size:21px;font-weight:650;margin:18px 0 6px;display:flex;align-items:center;gap:10px}
    .sub{color:var(--mut);font-size:13.5px}
    .box{margin-top:20px;background:rgba(0,0,0,.28);border:1px solid var(--brd);border-radius:12px;
      padding:14px 16px;font-size:13px;color:var(--mut);line-height:1.8}
    .box b{color:var(--fg);font-weight:600}
    .box code{color:var(--accent2);font:12px ui-monospace,Menlo,monospace;background:rgba(34,211,238,.08);
      padding:1px 6px;border-radius:6px}
    .btns{display:flex;gap:10px;margin-top:24px;flex-wrap:wrap}
    button{cursor:pointer;border:none;border-radius:11px;font-size:14px;font-weight:600;padding:11px 18px;
      font-family:inherit}
    .p{background:linear-gradient(135deg,var(--accent),#8B5CF6);color:#fff;flex:1;min-width:140px}
    .p:hover{filter:brightness(1.1)}
    .g{background:transparent;color:var(--fg);border:1px solid var(--brd);min-width:110px}
    .g:hover{background:rgba(255,255,255,.06)}
    .hint{margin-top:16px;color:var(--mut);font-size:12.5px}
  </style></head><body>
  <div class="card">
    <h1><span class="dot"></span>AI-EOS 服务未检测到</h1>
    <div class="sub">客户端是「壳」，需要一个正在运行的 AI-EOS 服务（前端 + 后端）。</div>
    <div class="box">
      正在尝试连接 <b>${safeFrontUrl}</b><br>
      ${repo
        ? "当前在开发环境：服务拉起失败，请查看 <code>logs/desktop-*.log</code>。"
        : "在运行 AI-EOS 的电脑上：<br>① 本机已启动 → 点「重试连接」<br>② 还没启动 → 运行「服务器一键部署」或便携版脚本<br>③ 远程服务器 → 点「更换服务器」填 <code>http://服务器IP:8000</code>"}
    </div>
    <div class="btns">
      <button class="p" onclick="window.aieosDesktop.retry()">重试连接</button>
      <button class="g" onclick="window.aieosDesktop.changeServer()">更换服务器</button>
      ${repo ? '<button class="g" onclick="window.aieosDesktop.openDataDir()">数据目录</button>' : ''}
    </div>
    <div class="hint">API 端点：${safeApiUrl} · 主进程日志：userData/logs/main.log</div>
  </div>
  </body></html>`;
}

// 统一入口：加载引导页（连接失败 / 子进程 spawn 失败都走这里）
function showGuide(reason) {
  if (reason) log(`显示引导页：${reason}`);
  // 服务不可达时小人页也打不开，留在桌面上只会是一个空白方块
  destroyPetWindow();
  if (!mainWindow) return; // 首次启动时 connectAndLoad 自己会兜底
  try {
    mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(guideHtml()));
  } catch (e) {
    log(`引导页加载失败：${e && e.message}`);
  }
}

// ---------- 导航守卫：只允许留在当前服务器（frontUrl）同源内 ----------
function frontOrigin() {
  try { return new URL(currentSettings().frontUrl).origin; } catch { return ""; }
}

function isInternalNav(target) {
  try {
    const u = new URL(target);
    if (u.protocol === "data:" || u.protocol === "about:") return true; // 引导页
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    if (u.origin === frontOrigin()) return true;
    // 开发场景可能加载的是 localhost:8000 而非 settings.frontUrl：当前页同源也放行
    try {
      if (mainWindow) return u.origin === new URL(mainWindow.webContents.getURL()).origin;
    } catch {}
    return false;
  } catch { return false; }
}

function openExternalSafe(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      log(`已拦截非 http(s) 外部跳转：${url}`);
      return;
    }
    shell.openExternal(url).catch((e) => log(`打开外部链接失败：${e && e.message}`));
  } catch (e) { log(`无效的外部链接：${url}`); }
}

// 远程页面（自托管服务器被投毒 / 前端 XSS）不能把主窗口导航走，更不能借此拿到 preload API
function guardNavigation(w) {
  const handler = (event, url) => {
    if (isInternalNav(url)) return;
    event.preventDefault();
    log(`拦截跨源导航 → ${url}`);
    openExternalSafe(url);
  };
  w.webContents.on("will-navigate", handler);
  w.webContents.on("will-redirect", handler);
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
    // mac 上用无边框标题栏，让工作台的三栏能顶到窗口边缘；红绿灯仍保留并把位置让出来
    ...(isMac ? { titleBarStyle: "hiddenInset", trafficLightPosition: { x: 16, y: 16 } } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      // preload 只用 require("electron") 的 contextBridge/ipcRenderer，沙箱下可用
      sandbox: true,
    },
  });
  w.webContents.setWindowOpenHandler(({ url }) => {
    openExternalSafe(url);
    return { action: "deny" };
  });
  guardNavigation(w);
  w.once("ready-to-show", () => { w.show(); });
  w.on("closed", () => {
    if (mainWindow === w) mainWindow = null;
    // 主窗口关掉后不再留一个无人管的小人窗：否则 Windows/Linux 上会变成
    // 「看不见界面但进程还在」，用户只能去任务管理器结束。
    destroyPetWindow();
  });
  mainWindow = w;
  return w;
}

// ---------- 常驻 CEO 小人（桌面宠物窗） ----------

function clampPetPos(p) {
  const x = Number.isFinite(p?.x) ? Math.round(p.x) : 0;
  const y = Number.isFinite(p?.y) ? Math.round(p.y) : 0;
  let display;
  try {
    display = screen.getDisplayNearestPoint({
      x: x + Math.round(PET_W / 2),
      y: y + Math.round(PET_H / 2),
    });
  } catch {
    display = screen.getPrimaryDisplay();
  }
  const a = display.workArea;
  return {
    x: Math.min(Math.max(a.x, x), Math.max(a.x, a.x + a.width - PET_W)),
    y: Math.min(Math.max(a.y, y), Math.max(a.y, a.y + a.height - PET_H)),
  };
}

function defaultPetPos() {
  const a = screen.getPrimaryDisplay().workArea;
  return clampPetPos({ x: a.x + a.width - PET_W - 24, y: a.y + a.height - PET_H - 20 });
}

function petPosition() {
  const saved = loadSettings().petPos;
  return saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)
    ? clampPetPos(saved)
    : defaultPetPos();
}

// 显示器拔插/分辨率变化后把小人拉回可见区域（否则会"消失"在已断开的屏幕上）
function keepPetOnScreen() {
  if (!petWindow || petWindow.isDestroyed()) return;
  const b = petWindow.getBounds();
  const p = clampPetPos({ x: b.x, y: b.y });
  if (p.x !== b.x || p.y !== b.y) petWindow.setPosition(p.x, p.y, false);
}

function createPetWindow() {
  if (petWindow && !petWindow.isDestroyed()) return petWindow;
  const pos = petPosition();
  const w = new BrowserWindow({
    width: PET_W,
    height: PET_H,
    x: pos.x,
    y: pos.y,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    resizable: false,
    movable: false, // 位置由主进程跟随光标计算，避免系统拖动和指针事件抢焦点
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    acceptFirstMouse: true, // mac：未激活时第一次点击也落到小人身上
    show: false,
    title: "AI-EOS CEO",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // floating 让小人浮在普通窗口之上，但不会盖住系统弹窗/输入法候选
  w.setAlwaysOnTop(true, isMac ? "floating" : "normal");
  if (isMac) w.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  w.setMenuBarVisibility(false);
  w.webContents.setWindowOpenHandler(({ url }) => {
    openExternalSafe(url);
    return { action: "deny" };
  });
  guardNavigation(w);
  w.on("blur", () => endPetDrag());
  w.on("closed", () => {
    if (petWindow === w) petWindow = null;
    stopPetDrag();
  });
  // 页面自身会再上报一次 has_ceo；这里先按主窗口的已知状态显示，避免空白窗
  w.webContents.once("did-finish-load", () => {
    if (petEligible && !w.isDestroyed()) w.showInactive();
  });
  petWindow = w;
  const url = `${currentSettings().frontUrl}/pet?desktop=1`;
  w.loadURL(url).catch((e) => log(`CEO 小人页加载失败：${e && e.message}`));
  log(`CEO 小人窗口已创建 → ${url}`);
  return w;
}

function destroyPetWindow() {
  stopPetDrag();
  petEligible = false;
  const w = petWindow;
  petWindow = null;
  if (w && !w.isDestroyed()) {
    try { w.destroy(); } catch (e) { log(`关闭 CEO 小人窗口失败：${e && e.message}`); }
  }
}

function ensurePetWindow() {
  if (loadSettings().petHidden === true) return { ok: true, hidden: true };
  petEligible = true;
  const w = createPetWindow();
  if (w && !w.isDestroyed() && !w.webContents.isLoading()) w.showInactive();
  return { ok: true, hidden: false };
}

function revealMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    connectAndLoad();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  if (!mainWindow.isVisible()) mainWindow.show();
  mainWindow.focus();
}

function openCeoChat() {
  revealMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("ceo:open"); } catch {}
  }
}

function toggleCeoChat() {
  revealMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("ceo:toggle"); } catch {}
  }
}

function startPetDrag() {
  if (!petWindow || petWindow.isDestroyed()) return;
  const cursor = screen.getCursorScreenPoint();
  const b = petWindow.getBounds();
  petDragOffset = { x: cursor.x - b.x, y: cursor.y - b.y };
  stopPetDragTimer();
  // 轮询光标而不是依赖渲染进程的 pointermove：窗口跟着光标走时，
  // 页面坐标系几乎不变，客户端事件量会骤减，拖动会一顿一顿。
  petDragTimer = setInterval(() => {
    if (!petWindow || petWindow.isDestroyed() || !petDragOffset) return stopPetDrag();
    const c = screen.getCursorScreenPoint();
    petWindow.setPosition(
      Math.round(c.x - petDragOffset.x),
      Math.round(c.y - petDragOffset.y),
      false,
    );
  }, 16);
}

function stopPetDragTimer() {
  if (petDragTimer) clearInterval(petDragTimer);
  petDragTimer = null;
}

function endPetDrag() {
  stopPetDragTimer();
  petDragOffset = null;
  if (!petWindow || petWindow.isDestroyed()) return;
  const b = petWindow.getBounds();
  const p = clampPetPos({ x: b.x, y: b.y });
  if (p.x !== b.x || p.y !== b.y) petWindow.setPosition(p.x, p.y, false);
  saveSettings({ petPos: p });
}

function stopPetDrag() {
  stopPetDragTimer();
  petDragOffset = null;
}

// ---------- 系统级快捷键（网页改了组合键会同步过来） ----------
const ACCEL_KEYS = {
  space: "Space",
  enter: "Return",
  esc: "Escape",
  escape: "Escape",
  tab: "Tab",
  up: "Up",
  down: "Down",
  left: "Left",
  right: "Right",
  comma: ",",
  period: ".",
  slash: "/",
  backslash: "\\",
  minus: "-",
  equal: "=",
  backquote: "`",
};

function acceleratorFromCombo(combo) {
  const parts = String(combo || "").toLowerCase().split("+").filter(Boolean);
  if (parts.length < 2) return "";
  const rawKey = parts.pop();
  const mods = [];
  for (const p of parts) {
    if (p === "meta") mods.push("Command");
    else if (p === "ctrl") mods.push("Control");
    else if (p === "alt") mods.push("Alt");
    else if (p === "shift") mods.push("Shift");
    else return "";
  }
  // 至少一个非 Shift 修饰键：纯 Shift+K 会和正常打字冲突
  if (!mods.some((m) => m === "Command" || m === "Control" || m === "Alt")) return "";
  const key = ACCEL_KEYS[rawKey]
    || (/^[a-z0-9]$/.test(rawKey) ? rawKey.toUpperCase() : "")
    || (/^f([1-9]|1[0-9]|2[0-4])$/.test(rawKey) ? rawKey.toUpperCase() : "");
  if (!key) return "";
  return [...mods, key].join("+");
}

function onGlobalCeoShortcut() {
  // 主窗口在前台时由页面自己的 keydown 处理，避免一次按键触发两遍
  if (mainWindow && !mainWindow.isDestroyed()
      && mainWindow.isVisible() && mainWindow.isFocused()) return;
  toggleCeoChat();
}

function applyCeoShortcut(combo) {
  const accel = acceleratorFromCombo(combo);
  if (!accel) return { ok: false, accelerator: "", error: "不支持的组合键" };
  if (accel === ceoAccelerator) return { ok: true, accelerator: accel };
  if (ceoAccelerator) {
    try { globalShortcut.unregister(ceoAccelerator); } catch {}
    ceoAccelerator = "";
  }
  let ok = false;
  try { ok = globalShortcut.register(accel, onGlobalCeoShortcut); } catch { ok = false; }
  if (ok) ceoAccelerator = accel;
  else log(`CEO 全局快捷键注册失败（可能被系统或其他应用占用）：${accel}`);
  return ok
    ? { ok: true, accelerator: accel }
    : { ok: false, accelerator: "", error: `${accel} 被系统或其他应用占用，仅在应用内生效` };
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
      { type: "separator" },
      {
        label: "检查更新",
        click: () => checkNowSelf(),
      },
      {
        label: "📡 局域网共享（手机扫码接入）",
        click: () => {
          if (!mainWindow) return;
          mainWindow.webContents.executeJavaScript(
            "location.assign('/lan-share')").catch(() => {});
        },
      },
      {
        label: "🩺 系统诊断",
        click: () => {
          if (!mainWindow) return;
          mainWindow.webContents.executeJavaScript(
            "location.assign('/diagnostics')").catch(() => {});
        },
      },
      {
        label: "更换服务器地址",
        click: () => openServerWindow(),
      },
      {
        label: "重新连接服务",
        click: () => { connectAndLoad(); },
      },
      { type: "separator" },
      {
        label: "唤起 CEO",
        click: () => toggleCeoChat(),
      },
      {
        label: "显示桌面 CEO 小人",
        type: "checkbox",
        checked: loadSettings().petHidden !== true,
        click: (item) => {
          if (item.checked) {
            saveSettings({ petHidden: false });
            ensurePetWindow();
          } else {
            saveSettings({ petHidden: true });
            destroyPetWindow();
          }
        },
      },
    ],
  };
  const about = {
    label: "关于 AI-EOS",
    click: () => dialog.showMessageBox(mainWindow, {
      title: "关于",
      message: "AI-EOS · 人工智能企业操作系统",
      detail: "桌面客户端 V5.1。\n后端：FastAPI · 前端：Next.js\n数据：SQLite 单文件（data/ 目录）",
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

// ---------- 更换服务器地址（小窗口表单；Electron 的 showMessageBox 没有 inputBox） ----------
let serverWin = null;

function serverFormHtml(current) {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0B0E14;color:#F5F8FF;font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      padding:24px}
    h1{font-size:16px;font-weight:650;margin-bottom:6px}
    .sub{color:#8B93A7;font-size:12.5px;margin-bottom:16px}
    input{width:100%;padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.14);
      background:rgba(0,0,0,.3);color:#F5F8FF;font:13px ui-monospace,Menlo,monospace;outline:none}
    input:focus{border-color:#6366F1}
    .err{color:#F87171;font-size:12.5px;min-height:18px;margin-top:8px}
    .btns{display:flex;gap:10px;justify-content:flex-end;margin-top:8px}
    button{cursor:pointer;border:none;border-radius:10px;font:13px/1 inherit;font-weight:600;padding:10px 16px}
    .g{background:transparent;color:#F5F8FF;border:1px solid rgba(255,255,255,.14)}
    .p{background:linear-gradient(135deg,#6366F1,#8B5CF6);color:#fff}
  </style></head><body>
    <h1>更换服务器地址</h1>
    <div class="sub">填写运行 AI-EOS 服务的前端地址（如 http://192.168.1.10:8000），留空恢复本机默认。</div>
    <input id="u" spellcheck="false" placeholder="http://192.168.1.10:8000" value="${escapeHtml(current)}">
    <div class="err" id="e"></div>
    <div class="btns">
      <button class="g" onclick="window.close()">取消</button>
      <button class="p" id="ok">保存</button>
    </div>
    <script>
      const u = document.getElementById('u'), e = document.getElementById('e');
      async function save() {
        e.textContent = '';
        try {
          const r = await window.aieosDesktop.submitServer(u.value);
          if (!r || !r.ok) e.textContent = (r && r.error) || '保存失败';
        } catch (err) { e.textContent = String(err && err.message || err); }
      }
      document.getElementById('ok').onclick = save;
      u.onkeydown = (ev) => { if (ev.key === 'Enter') save(); };
      u.focus(); u.select();
    </script>
  </body></html>`;
}

function openServerWindow() {
  if (serverWin) { serverWin.focus(); return; }
  const { frontUrl } = currentSettings();
  serverWin = new BrowserWindow({
    width: 520,
    height: 300,
    parent: mainWindow || undefined,
    modal: !!mainWindow,
    resizable: false,
    minimizable: false,
    maximizable: false,
    show: false,
    title: "更换服务器地址",
    backgroundColor: "#0b0e14",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  serverWin.setMenuBarVisibility(false);
  serverWin.once("ready-to-show", () => { if (serverWin) serverWin.show(); });
  serverWin.on("closed", () => { serverWin = null; });
  serverWin.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(serverFormHtml(frontUrl)));
}

// ---------- 自动升级（更新源：你自己服务器上的 /clients/update/*） ----------
function guessApiBase() {
  const s = loadSettings();
  if (s.apiBase) return String(s.apiBase).replace(/\/$/, "");
  const { frontUrl } = currentSettings();
  try {
    const u = new URL(frontUrl);
    // 便携/一键部署约定：前端 8000（或未显式指定端口）→ API 8001；其余同端口
    const isFrontDefault = u.port === "" || u.port === "8000";
    const apiPort = isFrontDefault ? "8001" : (u.port || "8000");
    return `${u.protocol}//${u.hostname}:${apiPort}`;
  } catch { return frontUrl; }
}

async function checkUpdateRemote() {
  const base = guessApiBase();
  const url = `${base}/clients/update/check?platform=${process.platform}&arch=${process.arch}&current=${app.getVersion()}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function updateEvents(payload) {
  if (mainWindow) mainWindow.webContents.send("update:event", payload);
}

async function sha256OfFile(p) {
  const crypto = require("crypto");
  const h = crypto.createHash("sha256");
  await new Promise((res, rej) => {
    fs.createReadStream(p).pipe(h).on("end", res).on("error", rej);
  });
  return h.digest("hex");
}

async function downloadToFile(url, dest, onProgress) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  // 下载不设硬超时（百 MB 级安装器在慢网下 5 分钟不够，中途 abort 只会逼用户重试）；
  // check 请求的 8s 超时保留在 checkUpdateRemote 里
  const res = await fetch(url);
  if (!res.ok) throw new Error(`下载失败 HTTP ${res.status}`);
  // fetch 默认跟随 30x：必须校验重定向后的最终地址，否则白名单可被一次跳转绕过
  try { assertUpdateUrl(res.url); } catch (e) {
    throw new Error(`下载被重定向到不受信任的地址（${res.url || "?"}），已中止`);
  }
  const total = Number(res.headers.get("content-length") || 0);
  const fileStream = fs.createWriteStream(dest);
  let got = 0;
  for await (const chunk of res.body) {
    got += chunk.length;
    fileStream.write(chunk);
    if (total) onProgress && onProgress(Math.round((got / total) * 100));
  }
  await new Promise((r, j) => { fileStream.end(() => fileStream.closed ? r() : fileStream.on("close", r)); fileStream.on("error", j); });
  return got;
}

// ---------- 升级包校验（不信任渲染进程传来的任何字段） ----------
// 下载地址白名单：只认当前「更新源」——settings.apiBase / apiUrl / frontUrl 的 host。
// https 强制；http 仅放行本机回环（且回环地址必须本来就是更新源，便携版 8001 即属此类）。
const UPDATE_EXT = {
  win32: [".exe"],
  darwin: [".dmg", ".zip"],
  linux: [".appimage", ".deb", ".zip", ".tar.gz"],
};

function isLoopbackHost(h) {
  const host = String(h || "").toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
}

function updateSourceHosts() {
  const s = loadSettings();
  const { frontUrl, apiUrl } = currentSettings();
  const hosts = new Set();
  for (const base of [s.apiBase, apiUrl, frontUrl]) {
    if (!base) continue;
    try {
      const u = new URL(String(base));
      if (u.protocol !== "http:" && u.protocol !== "https:") continue;
      hosts.add(u.hostname.toLowerCase());
    } catch { /* 无效配置直接跳过 */ }
  }
  return hosts;
}

function assertUpdateUrl(raw) {
  let u;
  try { u = new URL(String(raw || "")); } catch { throw new Error("更新地址无效"); }
  if (u.username || u.password) throw new Error("更新地址不得包含用户名/密码");
  const host = u.hostname.toLowerCase();
  const hosts = updateSourceHosts();
  if (!hosts.has(host)) throw new Error(`更新地址不在可信更新源内：${host}`);
  if (u.protocol === "https:") return u;
  // http 只允许回环，且回环必须出现在白名单里（= 更新源本身就是本机）
  if (u.protocol === "http:" && isLoopbackHost(host) && hosts.has(host)) return u;
  throw new Error("更新地址必须使用 https（仅本机回环允许 http）");
}

// 只接受纯文件名：basename 后仍含路径分隔符/.. /隐藏文件一律拒绝；再按平台白名单校验扩展名
function sanitizeUpdateFile(raw) {
  const name = String(raw || "");
  if (!name || name.length > 180) throw new Error("安装包文件名无效");
  if (name !== path.basename(name)) throw new Error("安装包文件名不得包含路径");
  if (name.includes("..") || /[\\/\u0000]/.test(name) || name.startsWith(".")) {
    throw new Error("安装包文件名非法");
  }
  const exts = UPDATE_EXT[process.platform] || [];
  const lower = name.toLowerCase();
  if (!exts.some((ext) => lower.endsWith(ext))) {
    throw new Error(`不支持的安装包类型：${name}`);
  }
  return name;
}

// manifest 必须带 64 位十六进制 SHA-256；缺失即拒绝升级（不再"没校验也照装"）
function sanitizeSha256(raw) {
  const v = String(raw || "").trim().toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(v)) throw new Error("缺少有效的 SHA-256 校验值，已拒绝升级");
  return v;
}

function updatesDir() { return path.join(app.getPath("userData"), "updates"); }

function assertInsideUpdatesDir(dest) {
  const base = path.resolve(updatesDir());
  const target = path.resolve(dest);
  if (target !== base && !target.startsWith(base + path.sep)) {
    throw new Error("安装包路径越界，已拒绝");
  }
  return target;
}

async function applyUpdate(file) {
  const dest = assertInsideUpdatesDir(path.join(updatesDir(), sanitizeUpdateFile(file)));
  if (isWin) {
    // NSIS 静默覆盖安装 → 等安装进程退出后再重启（否则 relaunch 与安装器争抢 exe）
    updateEvents({ phase: "installing", file });
    let restarted = false;
    let installFailed = false;
    const doRelaunch = () => {
      if (restarted || installFailed) return;
      restarted = true;
      killProc(apiProc);
      killProc(frontProc);
      app.relaunch();
      app.exit(0);
    };
    const p = spawn(dest, ["/S"], { detached: true, stdio: "ignore" });
    p.on("exit", () => setTimeout(doRelaunch, 800));
    p.on("error", (e) => {
      // spawn 失败（杀软锁定/权限不足）：不重启，交回手动路径可重试
      installFailed = true;
      _updating = false;
      updateEvents({ phase: "error", error: "安装器启动失败：" + (e && e.message || e) });
    });
    p.unref();
    setTimeout(doRelaunch, 120000); // 兜底：安装卡死时 120s 后仍重启
    return;
  }
  if (isMac) {
    // dmg/zip：挂载后引导替换（无法无管理员密码静默替换 /Applications）
    await shell.openPath(dest);
    updateEvents({ phase: "manual", dest, tip: file.endsWith(".dmg")
      ? "安装器已打开：把 AI-EOS 拖到「应用程序」替换旧版即可"
      : "请解压并把 AI-EOS.app 替换到 应用程序 目录" });
    _updating = false; // 手动路径结束 → 允许重试
    return;
  }
  // Linux：AppImage 场景尝试原地替换
  if (/AI-EOS.*\.AppImage$/.test(process.execPath)) {
    try {
      fs.renameSync(dest, process.execPath);
      updateEvents({ phase: "restarting" });
      killProc(apiProc); killProc(frontProc);
      app.relaunch();
      app.exit(0);
      return;
    } catch { /* 权限不足 → 走手动 */ }
  }
  updateEvents({ phase: "manual", dest, tip: "下载完成。请用该文件替换原 AppImage / 重新安装 deb" });
  _updating = false; // 手动路径结束 → 允许重试
}

async function runUpdateFlow(rawInfo) {
  if (_updating) return { ok: false, error: "已有更新在下载中" };
  // 渲染进程只能提供 {file, url, sha256} 三个字段，其余一律忽略；三项都过校验才下载
  let info;
  try {
    const src = rawInfo && typeof rawInfo === "object" ? rawInfo : {};
    info = {
      file: sanitizeUpdateFile(src.file),
      url: assertUpdateUrl(src.url).toString(),
      sha256: sanitizeSha256(src.sha256),
    };
  } catch (e) {
    const msg = String(e && e.message || e);
    log(`拒绝升级请求：${msg}`);
    updateEvents({ phase: "error", error: msg });
    return { ok: false, error: msg };
  }
  _updating = true;
  try {
    updateEvents({ phase: "downloading", pct: 0, file: info.file });
    const dest = assertInsideUpdatesDir(path.join(updatesDir(), info.file));
    const bytes = await downloadToFile(info.url, dest,
      (pct) => updateEvents({ phase: "downloading", pct, file: info.file }));
    // 完整性校验：sha256 必填，下载文件必须逐位一致才允许执行
    const got = await sha256OfFile(dest);
    if (got.toLowerCase() !== info.sha256) {
      try { fs.unlinkSync(dest); } catch {}
      throw new Error("安装包完整性校验失败（SHA-256 不一致），已放弃本次升级");
    }
    updateEvents({ phase: "done", file: info.file, bytes });
    await applyUpdate(info.file);
    return { ok: true };
  } catch (e) {
    _updating = false;
    updateEvents({ phase: "error", error: String(e && e.message || e) });
    return { ok: false, error: String(e && e.message || e) };
  }
}

function checkNowSelf() {
  checkUpdateRemote().then((info) => {
    if (info.has_update && info.url) {
      const target = guessApiBase() + info.url;
      // 清单缺 sha256 时不弹"立即升级"：宁可提示服务端补清单，也不装无校验的包
      if (!/^[a-f0-9]{64}$/i.test(String(info.sha256 || ""))) {
        dialog.showMessageBox(mainWindow, {
          title: "升级清单不完整",
          message: "更新清单缺少 SHA-256 校验值",
          detail: "为防止安装被篡改的安装包，已拒绝升级。请在服务器 /clients/update 清单中补上 sha256 后重试。",
          buttons: ["好"],
        });
        return;
      }
      dialog.showMessageBox(mainWindow, {
        title: "发现新版本",
        message: `AI-EOS ${info.latest} 可用（当前 ${app.getVersion()}）`,
        detail: (info.notes || "").slice(0, 300) + "\n\n现在下载并升级？",
        buttons: ["稍后", "立即升级"],
      }).then((r) => {
        if (r.response === 1) runUpdateFlow({ url: target, file: info.file, sha256: info.sha256 });
      });
    } else {
      dialog.showMessageBox(mainWindow, {
        title: "已是最新版",
        message: `AI-EOS ${app.getVersion()} 已是最新版本`,
        detail: info.message || "",
        buttons: ["好"],
      });
    }
  }).catch((e) => {
    dialog.showMessageBox(mainWindow, {
      title: "检查更新失败",
      message: "无法连接更新通道",
      detail: `${guessApiBase()}\n${e && e.message}`,
      buttons: ["好"],
    });
  });
}

// ---------- IPC ----------
function registerIpc() {
  ipcMain.handle("desktop:env", () => {
    const s = currentSettings();
    return {
      platform: process.platform,
      arch: process.arch,
      api: s.apiUrl,
      front: s.frontUrl,
      dataDir: DATA_DIR,
      repo: REPO_PRESENT,
      version: app.getVersion(),
      // 局域网共享状态（默认关闭 = 后端只听 127.0.0.1）
      lanShareEnabled: loadSettings().lanShareEnabled === true,
      apiBindHost: apiBindHost(),
    };
  });
  ipcMain.handle("desktop:openDataDir", async () => {
    await shell.openPath(DATA_DIR);
  });
  ipcMain.handle("desktop:retry", async () => {
    if (mainWindow) mainWindow.webContents.reload();
    await connectAndLoad();
  });
  ipcMain.handle("desktop:changeServer", async () => {
    openServerWindow();
  });
  // 更换服务器表单提交：只接受来自表单窗口的调用，主进程重新校验 URL
  ipcMain.handle("desktop:saveServerUrl", async (e, raw) => {
    if (!serverWin || BrowserWindow.fromWebContents(e.sender) !== serverWin) {
      return { ok: false, error: "无效的调用来源" };
    }
    const url = String(raw || "").trim().replace(/\/+$/, "");
    if (url) {
      let u;
      try { u = new URL(url); } catch { return { ok: false, error: "地址格式不正确，例如 http://192.168.1.10:8000" }; }
      if (u.protocol !== "http:" && u.protocol !== "https:") {
        return { ok: false, error: "只支持 http:// 或 https:// 地址" };
      }
      if (u.username || u.password) return { ok: false, error: "地址不能包含用户名/密码" };
    }
    saveSettings({ frontUrl: url, apiUrl: "" });
    log(`服务器地址已更新为 ${url || "（本机默认）"}（重启后生效）`);
    if (serverWin) serverWin.close();
    dialog.showMessageBox(mainWindow, {
      title: "已保存",
      message: "服务器地址已更新",
      detail: `新地址：${url || "本机默认"}\n需要重启客户端后生效。`,
      buttons: ["稍后", "立即重启"],
    }).then((r) => {
      if (r.response === 1) {
        killProc(apiProc); killProc(frontProc);
        app.relaunch();
        app.exit(0);
      }
    });
    return { ok: true, needsRestart: true };
  });
  // 自动升级：前端横幅 / 菜单 都能触发；runUpdateFlow 内部只取 {file,url,sha256} 并全量校验
  ipcMain.handle("desktop:startUpdate", (e, info) => {
    return runUpdateFlow(info);
  });
  ipcMain.handle("desktop:checkNow", () => {
    checkNowSelf();
    return true;
  });
  // 开机自启（V5.3）：mac/win 用系统登录项，linux 写 XDG autostart
  ipcMain.handle("desktop:getAutoLaunch", () => {
    try {
      if (process.platform === "linux") {
        return fs.existsSync(path.join(os.homedir(), ".config/autostart/aieos.desktop"));
      }
      return app.getLoginItemSettings().openAtLogin;
    } catch { return false; }
  });
  ipcMain.handle("desktop:setAutoLaunch", (_e, enabled) => {
    const on = !!enabled;
    try {
      if (process.platform === "linux") {
        const dir = path.join(os.homedir(), ".config/autostart");
        fs.mkdirSync(dir, { recursive: true });
        const f = path.join(dir, "aieos.desktop");
        if (on) {
          fs.writeFileSync(f, [
            "[Desktop Entry]",
            "Type=Application",
            "Name=AI-EOS",
            "Comment=AI-EOS 桌面版",
            `Exec=${process.execPath}`,
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
          ].join("\n"));
        } else if (fs.existsSync(f)) {
          fs.unlinkSync(f);
        }
      } else {
        app.setLoginItemSettings({ openAtLogin: on });
      }
    } catch (e) { log(`设置开机自启失败: ${e.message}`); }
    try { return ipcGetAutoLaunch(); } catch { return on; }
  });
  // 局域网共享开关（持久化在 userData/settings.json，默认关闭）
  // 打开 → 后端 --host 0.0.0.0（同局域网设备可接入）；关闭 → 127.0.0.1（仅本机）。
  // 绑定地址在 uvicorn 启动时确定，所以切换后需要重启后端进程才能生效。
  ipcMain.handle("desktop:getLanShare", () => ({
    enabled: loadSettings().lanShareEnabled === true,
    host: apiBindHost(),
    port: DEFAULT_API_PORT,
    repo: REPO_PRESENT,
  }));
  ipcMain.handle("desktop:setLanShare", async (_e, enabled) => {
    const on = !!enabled;
    saveSettings({ lanShareEnabled: on });
    let restarted = false;
    if (REPO_PRESENT && apiProc && !apiProc.killed) {
      try {
        killProc(apiProc);
        apiProc = null;
        startApi();
        restarted = await waitReady(`http://127.0.0.1:${DEFAULT_API_PORT}/health`, 30, 1000);
        log(`局域网共享${on ? "已开启" : "已关闭"}，后端监听 ${apiBindHost()}（重启${restarted ? "成功" : "未就绪"}）`);
      } catch (e) {
        log(`切换局域网共享后重启后端失败: ${e && e.message}`);
      }
    } else {
      log(`局域网共享${on ? "已开启" : "已关闭"}，将在下次启动后端时生效（监听 ${apiBindHost()}）`);
    }
    return { enabled: on, host: apiBindHost(), restarted, needsRestart: !(REPO_PRESENT && apiProc) };
  });

  // ---------- 常驻 CEO 小人 ----------
  // 只接受主窗口 / 小人窗口的调用：远程页面即使被注入脚本，也不能借这些接口
  // 创建窗口、改全局快捷键或切换服务器配置。
  const trusted = (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    return !!w && (w === mainWindow || w === petWindow);
  };

  ipcMain.handle("desktop:ensurePet", (e) => {
    if (!trusted(e)) return { ok: false, error: "无效的调用来源" };
    return ensurePetWindow();
  });
  ipcMain.handle("desktop:removePet", (e) => {
    if (!trusted(e)) return { ok: false, error: "无效的调用来源" };
    destroyPetWindow();
    return { ok: true };
  });
  ipcMain.handle("desktop:petState", (e, state) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!petWindow || w !== petWindow) return { ok: false, error: "无效的调用来源" };
    // 没登录 / 没指定 CEO：直接收掉窗口，别在桌面上留一个不能对话的形象
    if (!state || state.hasCeo !== true) {
      destroyPetWindow();
      return { ok: true, hasCeo: false };
    }
    petEligible = true;
    if (!petWindow.isVisible()) petWindow.showInactive();
    return { ok: true, hasCeo: true };
  });
  ipcMain.handle("desktop:petDragStart", (e) => {
    if (!trusted(e)) return { ok: false };
    startPetDrag();
    return { ok: true };
  });
  ipcMain.handle("desktop:petDragEnd", (e) => {
    if (!trusted(e)) return { ok: false };
    endPetDrag();
    return { ok: true };
  });
  ipcMain.handle("desktop:openCeoChat", (e) => {
    if (!trusted(e)) return { ok: false, error: "无效的调用来源" };
    openCeoChat();
    return { ok: true };
  });
  ipcMain.handle("desktop:showMain", (e) => {
    if (!trusted(e)) return { ok: false, error: "无效的调用来源" };
    revealMainWindow();
    return { ok: true };
  });
  ipcMain.handle("desktop:setCeoShortcut", (e, combo) => {
    // 只有主窗口（CeoDock 设置面板）可以改系统级快捷键
    if (!mainWindow || BrowserWindow.fromWebContents(e.sender) !== mainWindow) {
      return { ok: false, error: "无效的调用来源" };
    }
    const raw = String(combo || "").trim().toLowerCase();
    if (!raw) return { ok: false, error: "快捷键不能为空" };
    const res = applyCeoShortcut(raw);
    // 每次挂载页面都会同步一次；只有真的变了才落盘，避免无谓写文件
    if (res.ok && loadSettings().ceoShortcut !== raw) {
      saveSettings({ ceoShortcut: raw });
      log(`CEO 全局快捷键已更新：${res.accelerator}`);
    }
    return res;
  });
  ipcMain.handle("desktop:getCeoShortcut", () => ({
    combo: loadSettings().ceoShortcut || DEFAULT_CEO_COMBO,
    accelerator: ceoAccelerator,
    ok: !!ceoAccelerator,
  }));
}

function ipcGetAutoLaunch() {
  if (process.platform === "linux") {
    return fs.existsSync(path.join(os.homedir(), ".config/autostart/aieos.desktop"));
  }
  return app.getLoginItemSettings().openAtLogin;
}

// ---------- 清理子进程 ----------
function killProc(p) {
  if (!p || p.killed) return;
  try {
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

// ---------- 连接并加载（可重复调用：启动 / 重试 / 换服务器 共用） ----------
async function connectAndLoad() {
  const s = currentSettings();

  // 1) 目标前端在监听 → 直接加载
  if (await probe(s.frontUrl + "/", 2500)) {
    log(`前端 ${s.frontUrl} 就绪，加载`);
    if (!mainWindow) createWindow();
    mainWindow.loadURL(s.frontUrl);
    return true;
  }

  // 2) 开发场景：仓库在 → 复用/自拉起服务后重试
  if (REPO_PRESENT) {
    if (await portInUse(DEFAULT_API_PORT)) {
      log(`检测到 ${DEFAULT_API_PORT} 已有后端，复用（dev 模式）`);
    } else {
      startApi();
      const apiOk = await waitReady(`http://localhost:${DEFAULT_API_PORT}/health`, 60, 2000);
      if (!apiOk) {
        log("后端未就绪");
      }
    }
    if (await probe(`http://localhost:${DEFAULT_FRONT_PORT}/`, 1500)) {
      if (!mainWindow) createWindow();
      mainWindow.loadURL(`http://localhost:${DEFAULT_FRONT_PORT}`);
      return true;
    }
    await startFront();
    const ok = await waitReady(`http://localhost:${DEFAULT_FRONT_PORT}`, 90, 2000);
    if (ok) {
      if (!mainWindow) createWindow();
      mainWindow.loadURL(`http://localhost:${DEFAULT_FRONT_PORT}`);
      return true;
    }
  }

  // 3) 服务仍不可达 → 引导页
  if (!mainWindow) createWindow();
  showGuide("服务不可达");
  return false;
}

app.whenReady().then(async () => {
  ensureDirs();
  buildMenu();
  registerIpc();

  // 全局快捷键先按默认值注册；网页读到自己保存的组合后会通过 IPC 纠正。
  // 失败不阻塞启动：应用内快捷键仍然可用。
  applyCeoShortcut(loadSettings().ceoShortcut || DEFAULT_CEO_COMBO);
  for (const ev of ["display-added", "display-removed", "display-metrics-changed"]) {
    screen.on(ev, keepPetOnScreen);
  }

  if (SMOKE) {
    createWindow();
    mainWindow.loadURL("about:blank");
    log("[SMOKE] 主进程流程 OK（menu+ipc+window 创建无异常），5s 后自动退出");
    setTimeout(() => { killProc(apiProc); killProc(frontProc); app.quit(); }, 5000);
    return;
  }

  await connectAndLoad();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
    connectAndLoad();
  }
});

app.on("before-quit", () => { quitting = true; });
app.on("will-quit", (e) => {
  try { globalShortcut.unregisterAll(); } catch {}
  destroyPetWindow();
  if (!quitting || _shuttingDown) return;
  _shuttingDown = true;
  e.preventDefault();
  killProc(apiProc);
  killProc(frontProc);
  setTimeout(() => process.exit(0), 500);
});
process.on("SIGINT", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
process.on("SIGTERM", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
