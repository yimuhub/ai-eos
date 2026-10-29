"use client";

// 「单界面」应用标签的桥。
//
// 功能页面不再整页跳走，而是在工作台右栏以 iframe 标签打开。
// 侧边栏、⌘K、以及任何页面里的入口都只调用 `openApp(href)`：
//   - 工作台已挂载 → 交给它开标签（对话、左栏状态都不丢）；
//   - 工作台没挂载（登录前、桌面壳首次渲染）→ 退化成普通跳转。

import { ALL_PAGES, type NavItem } from "@/lib/nav";
import { readUIMode } from "@/lib/ui-mode";

export type AppTab = {
  /** 打开时的路径，作为标签的稳定身份（页面内部跳转不改它） */
  id: string;
  /** 标签当前实际所在路径（可被 iframe 内跳转改写） */
  href: string;
  /**
   * iframe 的加载地址，只在开标签/恢复标签时定下来。
   * 不能绑 href：内层跳转后 href 会变，改 src 会让页面被重新加载一次。
   */
  src: string;
  label: string;
  icon: string;
};

export type NavMatch = NavItem & { group: string };

/** 路径 → 导航项；详情页回退到最长的父级前缀。 */
export function navFor(href: string): NavMatch | null {
  const path = href.split("?")[0].split("#")[0];
  const exact = ALL_PAGES.find((p) => p.href === path);
  if (exact) return exact;
  const parents = ALL_PAGES
    .filter((p) => p.href !== "/" && path.startsWith(p.href + "/"))
    .sort((a, b) => b.href.length - a.href.length);
  return parents[0] ?? null;
}

/** 给内嵌页面加 `embed=1`：外壳据此去掉侧边栏与桌面标题栏留白。 */
export function embedURL(href: string): string {
  if (/(^|[?&])embed=1(&|$)/.test(href)) return href;
  return `${href}${href.includes("?") ? "&" : "?"}embed=1`;
}

export function stripEmbed(href: string): string {
  return href
    .replace(/([?&])embed=1&?/, "$1")
    .replace(/[?&]$/, "")
    .replace(/\?&/, "?");
}

let opener: ((href: string) => void) | null = null;
/** 经典模式下的普通路由跳转（由 ClassicLayout 注册，保留 SPA 体验） */
let navigator: ((href: string) => void) | null = null;

/** 工作台注册自己作为标签容器；返回注销函数，卸载时调用。 */
export function registerAppOpener(fn: (href: string) => void): () => void {
  opener = fn;
  return () => {
    if (opener === fn) opener = null;
  };
}

export function registerAppNavigator(fn: (href: string) => void): () => void {
  navigator = fn;
  return () => {
    if (navigator === fn) navigator = null;
  };
}

export function openApp(href: string) {
  if (!href) return;
  // 经典模式没有标签容器：一律整页跳转
  if (readUIMode() === "classic") {
    (navigator ?? ((h: string) => window.location.assign(h)))(href);
    return;
  }
  if (opener) {
    opener(href);
    return;
  }
  window.location.assign(href);
}

/** 当前是否在右栏 iframe 里（外壳据此隐藏全局侧边栏）。 */
export function isEmbedded(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.self !== window.top;
  } catch {
    // 跨域父窗口读 self/top 会抛错——能抛错就说明确实被嵌着
    return true;
  }
}
