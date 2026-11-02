/**
 * Electron preload 在 window.aieosDesktop 上暴露的桌面能力。
 *
 * 浏览器里返回 null，调用方据此退化成网页行为。类型只覆盖 CEO 常驻小人
 * 相关的方法；其余桌面接口（更新、局域网共享等）仍由各页面按需读 window。
 */
export type DesktopBridge = {
  isDesktop?: boolean;
  ensurePet?: () => Promise<{ ok?: boolean; hidden?: boolean; error?: string }>;
  removePet?: () => Promise<{ ok?: boolean }>;
  petState?: (state: { hasCeo: boolean }) => Promise<unknown>;
  petDragStart?: () => Promise<unknown>;
  petDragEnd?: () => Promise<unknown>;
  openCeoChat?: () => Promise<unknown>;
  showMain?: () => Promise<unknown>;
  setCeoShortcut?: (
    combo: string,
  ) => Promise<{ ok?: boolean; error?: string; accelerator?: string }>;
  getCeoShortcut?: () => Promise<{ combo?: string; accelerator?: string; ok?: boolean }>;
  onCeoOpen?: (cb: () => void) => (() => void) | void;
  onCeoToggle?: (cb: () => void) => (() => void) | void;
};

export function desktopBridge(): DesktopBridge | null {
  if (typeof window === "undefined") return null;
  const b = (window as any).aieosDesktop as DesktopBridge | undefined;
  return b?.isDesktop ? b : null;
}
