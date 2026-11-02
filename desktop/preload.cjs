/**
 * Preload：在 contextIsolation 下安全地把桌面环境信息暴露给渲染进程。
 * Web 前端可通过 window.aieosDesktop 判断"我是否跑在桌面壳里"，
 * 引导页（data: URL）也可用它触发「重试 / 换服务器」。
 *
 * 自动升级（V5.2 / 加固）：
 *  - checkNow()      主进程按当前服务器推断 API 地址主动检查（菜单「检查更新」）
 *  - startUpdate(x)  下载并应用更新；x 只能是 { file, url, sha256 }，主进程会
 *                    重新校验文件名、URL 白名单与 SHA-256，缺一即拒绝
 *  - onUpdateEvent() 订阅升级进度事件 { phase: downloading|done|installing|restarting|manual|error, pct?, dest?, error? }
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aieosDesktop", {
  isDesktop: true,
  getEnv: () => ipcRenderer.invoke("desktop:env"),
  openDataDir: () => ipcRenderer.invoke("desktop:openDataDir"),
  retry: () => ipcRenderer.invoke("desktop:retry"),
  changeServer: () => ipcRenderer.invoke("desktop:changeServer"),
  // 仅「更换服务器地址」小窗口使用；主进程会校验 sender 是否是该窗口
  submitServer: (url) => ipcRenderer.invoke("desktop:saveServerUrl", String(url || "")),
  checkNow: () => ipcRenderer.invoke("desktop:checkNow"),
  startUpdate: (info) => ipcRenderer.invoke("desktop:startUpdate", info),
  onUpdateEvent: (cb) => {
    const handler = (_e, data) => cb(data);
    ipcRenderer.on("update:event", handler);
    return () => ipcRenderer.removeListener("update:event", handler);
  },
  // 开机自启（V5.3 局域网共享页的「随系统启动」开关）
  getAutoLaunch: () => ipcRenderer.invoke("desktop:getAutoLaunch"),
  setAutoLaunch: (on) => ipcRenderer.invoke("desktop:setAutoLaunch", on),
  // 局域网共享开关（V5.10：默认只监听 127.0.0.1，开启后才绑定 0.0.0.0）
  getLanShare: () => ipcRenderer.invoke("desktop:getLanShare"),
  setLanShare: (on) => ipcRenderer.invoke("desktop:setLanShare", on),

  // ---------- 常驻 CEO 小人（D 包） ----------
  // 主窗口（CeoDock）用 ensure/remove 跟随「是否已任命 CEO」；
  // 小人窗口（/pet）只负责上报状态、拖动与唤起对话，不接触服务器配置。
  ensurePet: () => ipcRenderer.invoke("desktop:ensurePet"),
  removePet: () => ipcRenderer.invoke("desktop:removePet"),
  petState: (state) => ipcRenderer.invoke("desktop:petState", {
    hasCeo: !!(state && state.hasCeo),
  }),
  petDragStart: () => ipcRenderer.invoke("desktop:petDragStart"),
  petDragEnd: () => ipcRenderer.invoke("desktop:petDragEnd"),
  openCeoChat: () => ipcRenderer.invoke("desktop:openCeoChat"),
  showMain: () => ipcRenderer.invoke("desktop:showMain"),
  // 网页里改快捷键后同步到系统级注册；combo 形如 meta+shift+k
  setCeoShortcut: (combo) => ipcRenderer.invoke("desktop:setCeoShortcut", String(combo || "")),
  getCeoShortcut: () => ipcRenderer.invoke("desktop:getCeoShortcut"),
  // 小人窗口点击 → 主进程把事件转给主窗口的 CeoDock（跨窗不能用 CustomEvent）
  onCeoOpen: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("ceo:open", handler);
    return () => ipcRenderer.removeListener("ceo:open", handler);
  },
  onCeoToggle: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("ceo:toggle", handler);
    return () => ipcRenderer.removeListener("ceo:toggle", handler);
  },
  platform: process.platform,
});
