"use client";

// 两种界面形态：
//   classic = v5.12 之前的「左侧固定菜单 + 整页导航」，一页一件事；
//   geek    = v5.13 的「/workspace 单外壳 + 右栏标签」，对话不中断。
//
// 默认是 classic：没有存储过选择（以及 SSR 首帧）一律按经典模式处理，
// 不再强制二选一；想用三栏极客模式的人可以随时从侧边栏 / ⌘K 切过去。
// 用 useSyncExternalStore 订阅，切换后所有挂着的组件（外壳、侧栏、⌘K）立即对齐。

import { useCallback, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";

export type UIMode = "classic" | "geek";

/** 未选择时的默认形态。SSR 与首帧也用它，避免"先空一帧再选"的闪烁。 */
export const DEFAULT_UI_MODE: UIMode = "classic";

const MODE_KEY = "aieos_ui_mode";
const EVENT = "aieos:ui-mode";

/** 当前模式。未存储 / 存储损坏 / SSR 一律回退到 classic。 */
export function readUIMode(): UIMode {
  if (typeof window === "undefined") return DEFAULT_UI_MODE;
  try {
    const v = localStorage.getItem(MODE_KEY);
    return v === "classic" || v === "geek" ? v : DEFAULT_UI_MODE;
  } catch {
    return DEFAULT_UI_MODE;
  }
}

export function writeUIMode(mode: UIMode) {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* 隐私模式写不进去：本次会话仍然生效，只是下次要重新选 */
  }
  window.dispatchEvent(new CustomEvent<UIMode>(EVENT, { detail: mode }));
}

function subscribe(onChange: () => void) {
  window.addEventListener(EVENT, onChange);
  // 多标签页之间也保持一致：另一个窗口改了模式，这里跟着切
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/** 当前模式；未选择时就是默认的 classic（SSR 也是 classic）。 */
export function useUIMode(): UIMode {
  return useSyncExternalStore(subscribe, readUIMode, () => DEFAULT_UI_MODE);
}

/**
 * 切换模式并做一次"落点修正"：
 *   经典 → 极客：普通页面带回 /workspace 当作标签打开；
 *   极客 → 经典：?app= 指向的页面直接整页打开，别停在空工作台上。
 */
export function useSwitchMode() {
  const router = useRouter();
  return useCallback((next: UIMode) => {
    const prev = readUIMode();
    writeUIMode(next);
    if (prev === next) return;
    if (next === "classic") {
      const app = new URLSearchParams(window.location.search).get("app");
      if (app && app.startsWith("/") && !app.startsWith("/workspace")) {
        router.replace(app);
      }
      return;
    }
    const path = window.location.pathname;
    if (path !== "/workspace" && path !== "/") {
      const target = `${path}${window.location.search}${window.location.hash}`;
      router.replace(`/workspace?app=${encodeURIComponent(target)}`);
    }
  }, [router]);
}
