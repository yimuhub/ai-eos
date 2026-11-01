// 界面模式语义：未设置 = classic，只有显式存过 geek 才是极客。
//
// 这条契约决定首次进入是否弹「二选一」，也决定 SSR 首帧渲染哪个外壳；
// 测试直接锁 readUIMode / writeUIMode 的存储与事件语义。
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_UI_MODE, readUIMode, writeUIMode } from "./ui-mode";

function stubBrowser(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  const dispatchEvent = vi.fn();
  const localStorage = {
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
  };
  class FakeCustomEvent<T> {
    type: string;
    detail: T;
    constructor(type: string, init?: { detail?: T }) {
      this.type = type;
      this.detail = init?.detail as T;
    }
  }
  vi.stubGlobal("window", { localStorage, dispatchEvent });
  vi.stubGlobal("localStorage", localStorage);
  vi.stubGlobal("CustomEvent", FakeCustomEvent);
  return { store, dispatchEvent };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("界面模式", () => {
  it("默认是 classic", () => {
    expect(DEFAULT_UI_MODE).toBe("classic");
  });

  it("SSR（没有 window）回退到 classic，而不是 null", () => {
    expect(typeof window).toBe("undefined");
    expect(readUIMode()).toBe("classic");
  });

  it("从未选择过时是 classic", () => {
    stubBrowser();
    expect(readUIMode()).toBe("classic");
  });

  it("存储过 geek 时返回 geek；存储过 classic 时返回 classic", () => {
    stubBrowser({ aieos_ui_mode: "geek" });
    expect(readUIMode()).toBe("geek");

    stubBrowser({ aieos_ui_mode: "classic" });
    expect(readUIMode()).toBe("classic");
  });

  it("存储值损坏时回退到 classic", () => {
    stubBrowser({ aieos_ui_mode: "banana" });
    expect(readUIMode()).toBe("classic");
  });

  it("writeUIMode 同时写存储并广播事件，供所有挂载组件对齐", () => {
    const { store, dispatchEvent } = stubBrowser();
    writeUIMode("geek");
    expect(store.get("aieos_ui_mode")).toBe("geek");
    expect(dispatchEvent).toHaveBeenCalledTimes(1);
    const event = dispatchEvent.mock.calls[0][0] as { type: string; detail: string };
    expect(event.type).toBe("aieos:ui-mode");
    expect(event.detail).toBe("geek");
    expect(readUIMode()).toBe("geek");

    writeUIMode("classic");
    expect(readUIMode()).toBe("classic");
  });

  it("隐私模式写不进去时仍广播本次切换", () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: () => null,
        setItem: () => {
          throw new Error("quota");
        },
      },
      dispatchEvent,
    });
    vi.stubGlobal("localStorage", window.localStorage);
    vi.stubGlobal("CustomEvent", class {
      type: string;
      detail: unknown;
      constructor(type: string, init?: { detail?: unknown }) {
        this.type = type;
        this.detail = init?.detail;
      }
    });

    expect(() => writeUIMode("geek")).not.toThrow();
    expect(dispatchEvent).toHaveBeenCalledTimes(1);
  });
});
