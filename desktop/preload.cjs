/**
 * Preload：在 contextIsolation 下安全地把桌面环境信息暴露给渲染进程。
 * Web 前端可通过 window.aieosDesktop 判断"我是否跑在桌面壳里"，
 * 例如显示"数据目录"入口、关闭浏览器标签提示等。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aieosDesktop", {
  isDesktop: true,
  getEnv: () => ipcRenderer.invoke("desktop:env"),
  openDataDir: () => ipcRenderer.invoke("desktop:openDataDir"),
  platform: process.platform,
});
