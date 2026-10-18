/**
 * Preload：在 contextIsolation 下安全地把桌面环境信息暴露给渲染进程。
 * Web 前端可通过 window.aieosDesktop 判断"我是否跑在桌面壳里"，
 * 引导页（data: URL）也可用它触发「重试 / 换服务器」。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aieosDesktop", {
  isDesktop: true,
  getEnv: () => ipcRenderer.invoke("desktop:env"),
  openDataDir: () => ipcRenderer.invoke("desktop:openDataDir"),
  retry: () => ipcRenderer.invoke("desktop:retry"),
  changeServer: () => ipcRenderer.invoke("desktop:changeServer"),
  platform: process.platform,
});
