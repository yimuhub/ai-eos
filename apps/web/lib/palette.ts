// 命令面板的轻量入口：只放「事件名 + 最近访问记录」，不 import 任何 React 组件。
//
// 为什么单独一个文件：
//   侧边栏、经典外壳、工作台都只需要「打开面板」这一个动作。如果它们从
//   `components/CommandPalette.tsx` 取函数，整个面板（及其检索逻辑）就会被
//   静态拉进首屏/共享 chunk；面板本身是 `next/dynamic` 按需加载的，入口也必须轻。
//
// 面板组件监听下面两个 window 事件；事件名是模块内约定，外部不要手写字符串。

/** 打开命令面板 */
export const OPEN_PALETTE_EVENT = "aieos:open-palette";
/** 打开「全部页面」视图（空查询按分组列出全部页面） */
export const OPEN_ALL_PAGES_EVENT = "aieos:open-all-pages";

/** 最近访问页面记录的存储 key（与历史版本保持一致，升级不丢数据） */
export const RECENT_PAGES_KEY = "aieos_recent_pages";
const RECENT_MAX = 6;

/** 打开命令面板。让侧边栏的搜索框、快捷键提示等入口不必各自管理弹窗状态。 */
export function openCommandPalette() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(OPEN_PALETTE_EVENT));
}

/** 打开「全部页面」视图：空查询时按分组列出全部页面，不只看最近访问。 */
export function openAllPages() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(OPEN_ALL_PAGES_EVENT));
}

/** 读取最近访问的页面路径（工作台打开标签、命令面板跳转时写入） */
export function readRecentPages(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(RECENT_PAGES_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** 记录一次访问。幂等、去重、只留最近 N 条。 */
export function pushRecentPage(href: string) {
  if (typeof window === "undefined") return;
  try {
    const list = readRecentPages().filter((h) => h !== href);
    list.unshift(href);
    localStorage.setItem(RECENT_PAGES_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
  } catch {
    /* 隐私模式下写不了 localStorage：最近访问失效，但不影响跳转本身 */
  }
}
