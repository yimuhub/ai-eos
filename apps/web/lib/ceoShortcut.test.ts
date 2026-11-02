// CEO 唤起快捷键：归一化、平台默认值、展示文案、本机配置读取。
//
// 这些判断同时被主窗口抽屉和 iframe 转发使用；一旦漂移，
// 会出现"在页面里按得开、点进标签就失灵"的怪现象。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CEO_SHORTCUT_STORE,
  canonicalCombo,
  comboLabel,
  defaultCeoShortcut,
  eventCombo,
  matchesCeoShortcut,
  readCeoShortcut,
} from "./ceoShortcut";

function keyEvent(init: Partial<KeyboardEvent>): KeyboardEvent {
  return {
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    key: "",
    ...init,
  } as KeyboardEvent;
}

function stubPlatform(platform: string, stored?: string) {
  vi.stubGlobal("navigator", { platform });
  const store = new Map<string, string>();
  if (stored) store.set(CEO_SHORTCUT_STORE, stored);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => store.set(k, String(v)),
  });
  return store;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CEO 唤起快捷键", () => {
  it("组合键顺序不影响匹配", () => {
    expect(canonicalCombo("shift+meta+K")).toBe("meta+shift+k");
    expect(canonicalCombo("meta+shift+k")).toBe("meta+shift+k");
  });

  it("⌘⇧K 与 ⌘K 不会混淆", () => {
    const combo = "meta+shift+k";
    expect(matchesCeoShortcut(keyEvent({ metaKey: true, shiftKey: true, key: "K" }), combo)).toBe(true);
    expect(matchesCeoShortcut(keyEvent({ metaKey: true, key: "k" }), combo)).toBe(false);
  });

  it("Windows 默认用 Ctrl+Shift+K", () => {
    stubPlatform("Win32");
    expect(defaultCeoShortcut()).toBe("ctrl+shift+k");
    expect(eventCombo(keyEvent({ ctrlKey: true, shiftKey: true, key: "k" }))).toBe("ctrl+shift+k");
  });

  it("mac 的展示文案用符号", () => {
    stubPlatform("MacIntel");
    expect(comboLabel("meta+shift+k", true)).toBe("⌘⇧K");
    expect(comboLabel("ctrl+shift+k", false)).toBe("Ctrl+Shift+K");
  });

  it("本机改过的组合优先于平台默认值", () => {
    const store = stubPlatform("MacIntel", "alt+k");
    expect(readCeoShortcut()).toBe("alt+k");
    store.delete(CEO_SHORTCUT_STORE);
    expect(readCeoShortcut()).toBe("meta+shift+k");
  });
});
