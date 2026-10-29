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
  platform: process.platform,
});
