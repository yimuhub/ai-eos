// CEO 常驻对话的快捷键约定。
//
// 默认 ⌘⇧K（Windows/Linux 为 Ctrl+Shift+K），可在 CEO 抽屉里改，
// 改完存 localStorage。这里把"读配置 / 按键归一化 / 展示文案"收在一处，
// 主窗口的抽屉和 iframe 里的转发逻辑共用，避免两套判断慢慢漂移。

export const CEO_SHORTCUT_STORE = "aieos_ceo_shortcut";

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Mac|iPhone|iPad/.test(navigator.platform || "");
}

export function defaultCeoShortcut(): string {
  return isMacPlatform() ? "meta+shift+k" : "ctrl+shift+k";
}

export function readCeoShortcut(): string {
  try {
    return localStorage.getItem(CEO_SHORTCUT_STORE) || defaultCeoShortcut();
  } catch {
    return defaultCeoShortcut();
  }
}

/** 修饰键排序后 + 主键，保证 `shift+meta+k` 与 `meta+shift+k` 等价 */
export function canonicalCombo(combo: string): string {
  const parts = combo.toLowerCase().split("+").filter(Boolean);
  const key = parts.pop() || "";
  return [...parts.sort(), key].join("+");
}

export function eventCombo(e: KeyboardEvent): string {
  const mods: string[] = [];
  if (e.metaKey) mods.push("meta");
  if (e.ctrlKey) mods.push("ctrl");
  if (e.altKey) mods.push("alt");
  if (e.shiftKey) mods.push("shift");
  return [...mods, e.key.toLowerCase()].join("+");
}

export function matchesCeoShortcut(e: KeyboardEvent, combo: string): boolean {
  if (!combo) return false;
  return canonicalCombo(eventCombo(e)) === canonicalCombo(combo);
}

export function comboLabel(combo: string, mac = isMacPlatform()): string {
  if (!combo) return "";
  const parts = combo.toLowerCase().split("+").filter(Boolean);
  const key = (parts.pop() || "").toUpperCase();
  const map: Record<string, string> = {
    meta: mac ? "⌘" : "Meta",
    ctrl: "Ctrl",
    alt: mac ? "⌥" : "Alt",
    shift: mac ? "⇧" : "Shift",
  };
  const mods = parts.map((p) => map[p] || p);
  return mac ? [...mods, key].join("") : [...mods, key].join("+");
}
