/**
 * 桌面壳（Electron preload）注入到 window 的 API 面。
 * 浏览器访问时 window.aieosDesktop 为 undefined——调用方一律 `?.` + 兜底。
 */
interface AieosDesktopApi {
  isDesktop: boolean;
  platform: string;
  getEnv: () => Promise<{ platform: string; arch: string; version: string }>;
  checkNow: () => Promise<boolean>;
  startUpdate: (info: {
    url: string; file: string; platform: string; sha256?: string;
  }) => Promise<{ ok: boolean; error?: string }>;
  onUpdateEvent: (cb: (e: any) => void) => () => void;
  retry: () => Promise<void>;
  changeServer: () => Promise<void>;
  openDataDir: () => Promise<void>;
  // V5.3 局域网共享页「开机自启」开关（mac/win 系统登录项 · linux XDG autostart）
  getAutoLaunch: () => Promise<boolean>;
  setAutoLaunch: (on: boolean) => Promise<boolean>;
}

interface Window {
  aieosDesktop?: AieosDesktopApi;
}
