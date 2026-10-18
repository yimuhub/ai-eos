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
const { app, BrowserWindow, Menu, shell, dialog, ipcMain } = require("electron");
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

let apiProc = null;
let frontProc = null;
let mainWindow = null;
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

// ---------- 启动便携后端（FastAPI + SQLite，仅仓库场景） ----------
function startApi() {
  const py = isWin ? "py" : "python3";
  const logFile = fs.openSync(path.join(LOG_DIR, "desktop-api.log"), "a");
  const env = {
    ...process.env,
    DATABASE_URL: `sqlite+aiosqlite:///${path.join(DATA_DIR, "aieos.db")}`,
    PYTHONUNBUFFERED: "1",
    PYTHONPATH: path.join(ROOT, "apps/api"),
  };
  const useUv = fs.existsSync(path.join(ROOT, ".venv")) || process.env.UV === "1";
  // 0.0.0.0：允许同局域网的手机/其他设备扫码接入（V5.3 局域网共享）；
  // 数据仍是本机 SQLite，访问控制靠登录鉴权（不对外开放匿名写接口）
  const args = useUv
    ? ["run", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", String(DEFAULT_API_PORT), "--log-level", "info"]
    : ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", String(DEFAULT_API_PORT), "--log-level", "info"];
  apiProc = spawn(useUv ? "uv" : py, args, {
    cwd: path.join(ROOT, "apps/api"),
    env,
    stdio: ["ignore", logFile, logFile],
    detached: !isMac,
  });
  log(`后端 uvicorn 已拉起 (PID ${apiProc.pid}) → http://localhost:${DEFAULT_API_PORT}`);
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
    await new Promise((res) => build.on("close", (code) => res(code === 0)));
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
  log(`前端 next 已拉起 (PID ${frontProc.pid}) → http://localhost:${DEFAULT_FRONT_PORT}`);
  return frontProc;
}

// ---------- 服务未检测到 → 引导页（玻璃拟态风格） ----------
function guideHtml() {
  const { frontUrl, apiUrl } = currentSettings();
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
      正在尝试连接 <b>${frontUrl}</b><br>
      ${repo
        ? "当前在开发环境：服务拉起失败，请查看 <code>logs/desktop-*.log</code>。"
        : "在运行 AI-EOS 的电脑上：<br>① 本机已启动 → 点「重试连接」<br>② 还没启动 → 运行「服务器一键部署」或便携版脚本<br>③ 远程服务器 → 点「更换服务器」填 <code>http://服务器IP:8000</code>"}
    </div>
    <div class="btns">
      <button class="p" onclick="window.aieosDesktop.retry()">重试连接</button>
      <button class="g" onclick="window.aieosDesktop.changeServer()">更换服务器</button>
      ${repo ? '<button class="g" onclick="window.aieosDesktop.openDataDir()">数据目录</button>' : ''}
    </div>
    <div class="hint">API 端点：${apiUrl} · 主进程日志：logs/desktop-*.log</div>
  </div>
  </body></html>`;
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
  w.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) shell.openExternal(url);
    return { action: "deny" };
  });
  w.once("ready-to-show", () => { w.show(); });
  mainWindow = w;
  return w;
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
        label: "更换服务器地址",
        click: () => promptServerAndApply(),
      },
      {
        label: "重新连接服务",
        click: () => { connectAndLoad(); },
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

// ---------- 更换服务器地址 ----------
function promptServerAndApply() {
  const { frontUrl } = currentSettings();
  dialog.showMessageBox(mainWindow, {
    title: "更换服务器地址",
    message: "当前服务器：" + frontUrl,
    detail: "输入新的前端服务地址（http://IP:端口），留空恢复本机默认。",
    buttons: ["取消", "确定"],
    inputBox: frontUrl,
  }).then((r) => {
    if (r.response === 1 && r.inputBox && r.inputBox.trim()) {
      saveSettings({ frontUrl: r.inputBox.trim().replace(/\/$/, ""), apiUrl: "" });
      connectAndLoad();
    }
  });
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

async function applyUpdate(file) {
  const dest = path.join(app.getPath("userData"), "updates", file);
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

async function runUpdateFlow(info) {
  if (_updating) return { ok: false, error: "已有更新在下载中" };
  _updating = true;
  try {
    updateEvents({ phase: "downloading", pct: 0, file: info.file });
    const dest = path.join(app.getPath("userData"), "updates", info.file);
    const bytes = await downloadToFile(info.url, dest,
      (pct) => updateEvents({ phase: "downloading", pct, file: info.file }));
    // 完整性校验：manifest 带 sha256 时，下载文件必须逐位一致才允许执行
    if (info.sha256) {
      const got = await sha256OfFile(dest);
      if (got.toLowerCase() !== String(info.sha256).toLowerCase()) {
        throw new Error("安装包完整性校验失败（SHA-256 不一致），已放弃本次升级");
      }
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
      dialog.showMessageBox(mainWindow, {
        title: "发现新版本",
        message: `AI-EOS ${info.latest} 可用（当前 ${app.getVersion()}）`,
        detail: (info.notes || "").slice(0, 300) + "\n\n现在下载并升级？",
        buttons: ["稍后", "立即升级"],
      }).then((r) => {
        if (r.response === 1) runUpdateFlow({ url: target, file: info.file, platform: process.platform, sha256: info.sha256 || undefined });
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
    promptServerAndApply();
  });
  // 自动升级：前端横幅 / 菜单 都能触发
  ipcMain.handle("desktop:startUpdate", (e, info) => {
    // info: { url, file, platform } —— 前端用自己的 API base 拼好完整 url
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
  log("服务不可达，显示引导页");
  if (!mainWindow) createWindow();
  mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(guideHtml()));
  return false;
}

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
  if (!quitting || _shuttingDown) return;
  _shuttingDown = true;
  e.preventDefault();
  killProc(apiProc);
  killProc(frontProc);
  setTimeout(() => process.exit(0), 500);
});
process.on("SIGINT", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
process.on("SIGTERM", () => { killProc(apiProc); killProc(frontProc); app.quit(); });
