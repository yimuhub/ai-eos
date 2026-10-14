// 全局快捷键：事件总线 + 快捷键清单。
//
// ## 设计
//
// 按键监听只做一件事：把按键翻译成 window CustomEvent 派发出去，
// 具体"按了 n 该干什么"由各页面自己订阅事件决定。
// 这样新增快捷键时不用改监听器，页面也不用知道按键细节。
//
// 快捷键只在"非输入状态"下生效（输入框/文本域/下拉里按 n 是在打字），
// 且绝不拦截 ⌘K——命令面板有自己独立的监听器，两边互不干扰。

export const SHORTCUT_EVENTS = {
  /** n：新建任务（任务中心页订阅，打开新建弹窗） */
  newTask: "aieos:shortcut:new-task",
  /** /：聚焦搜索（有搜索框的页面订阅，把焦点放进搜索框） */
  focusSearch: "aieos:shortcut:focus-search",
  /** j/k：列表导航。detail: { dir: 1 | -1 } */
  move: "aieos:shortcut:move",
  /** 空格：多选当前高亮项（批量模式下） */
  toggleSelect: "aieos:shortcut:toggle-select",
  /** 回车：打开当前高亮项 */
  openItem: "aieos:shortcut:open-item",
} as const;

export function emitShortcut(name: string, detail?: unknown) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

export function onShortcut(
  name: string,
  handler: (detail: any) => void,
): () => void {
  if (typeof window === "undefined") return () => {};
  const fn = (e: Event) => handler((e as CustomEvent).detail);
  window.addEventListener(name, fn);
  return () => window.removeEventListener(name, fn);
}

export type ShortcutItem = { key: string; label: string; desc: string };

/** 快捷键帮助面板与按键处理共用同一份清单，避免"文档与实现"漂移 */
export const SHORTCUTS: ShortcutItem[] = [
  { key: "n", label: "新建任务", desc: "在任务中心打开新建任务弹窗" },
  { key: "/", label: "聚焦搜索", desc: "把光标放进当前页面的搜索框" },
  { key: "j / k", label: "上下导航", desc: "在任务列表中移动高亮（j 下、k 上）" },
  { key: "空格", label: "多选", desc: "勾选/取消高亮的任务（批量删除模式下）" },
  { key: "↵", label: "打开", desc: "打开高亮任务的详情页" },
  { key: "?", label: "快捷键帮助", desc: "打开 / 关闭本面板" },
  { key: "⌘K", label: "全局搜索", desc: "原有命令面板，行为不变" },
];
