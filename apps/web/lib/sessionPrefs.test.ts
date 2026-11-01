// lib/sessionPrefs.ts 的测试：降级、置顶/归档、已读判定、scope 隔离与防抖写入。
//
// vitest 跑在 node 环境，没有 localStorage：每个用例注入一个内存 Storage，
// 用 fake timers 精确验证 200ms 防抖窗口。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SESSION_PREFS_DEBOUNCE_MS,
  SESSION_PREFS_VERSION,
  clearSessionPrefs,
  emptySessionPrefs,
  flushSessionPrefs,
  loadSessionPrefs,
  markRead,
  mergeReadAt,
  parseSessionPrefs,
  saveSessionPrefs,
  sessionPrefsStorageKey,
  toggleArchive,
  togglePin,
  unreadKeys,
  type SessionPrefs,
} from "./sessionPrefs";

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key: string) => (data.has(key) ? data.get(key)! : null),
    key: (index: number) => Array.from(data.keys())[index] ?? null,
    removeItem: (key: string) => {
      data.delete(key);
    },
    setItem: (key: string, value: string) => {
      data.set(key, String(value));
    },
  } as unknown as Storage;
}

const store = memoryStorage();

beforeEach(() => {
  vi.useFakeTimers();
  store.clear();
  vi.stubGlobal("window", { localStorage: store });
});

afterEach(() => {
  // 先落盘/清掉 pending，避免用例间互相污染（此时 window 仍是内存 Storage）
  flushSessionPrefs();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  store.clear();
});

function prefsWith(partial: Partial<SessionPrefs>): SessionPrefs {
  return { ...emptySessionPrefs(), ...partial };
}

describe("解析与降级", () => {
  it("版本不匹配时直接降级为空（不做迁移）", () => {
    const raw = JSON.stringify({
      version: SESSION_PREFS_VERSION + 1,
      pinned: ["single:a"],
      archived: ["war:b"],
      readAt: { "single:a": 123 },
    });
    expect(parseSessionPrefs(raw)).toEqual(emptySessionPrefs());
    expect(parseSessionPrefs(JSON.stringify({ pinned: ["single:a"] }))).toEqual(emptySessionPrefs());
  });

  it("损坏 JSON / 非对象 / 空值都降级为空，绝不抛错", () => {
    expect(parseSessionPrefs("not-json{")).toEqual(emptySessionPrefs());
    expect(parseSessionPrefs("")).toEqual(emptySessionPrefs());
    expect(parseSessionPrefs("null")).toEqual(emptySessionPrefs());
    expect(parseSessionPrefs(JSON.stringify(["x"]))).toEqual(emptySessionPrefs());
    expect(parseSessionPrefs(null)).toEqual(emptySessionPrefs());
  });

  it("字段非法时清洗：去重、去空、丢掉坏时间戳", () => {
    const raw = JSON.stringify({
      version: SESSION_PREFS_VERSION,
      pinned: [" single:a ", "single:a", 42, "", "meeting:b"],
      archived: ["war:c", null],
      readAt: { "single:a": "1000", "meeting:b": -5, "war:c": "oops", "": 1 },
    });
    expect(parseSessionPrefs(raw)).toEqual({
      version: SESSION_PREFS_VERSION,
      pinned: ["single:a", "meeting:b"],
      archived: ["war:c"],
      readAt: { "single:a": 1000 },
    });
  });

  it("localStorage 里是坏数据时 load 返回空偏好（不能白屏）", () => {
    store.setItem(sessionPrefsStorageKey("c1::boss"), "{broken");
    expect(loadSessionPrefs("c1::boss")).toEqual(emptySessionPrefs());
    store.setItem(sessionPrefsStorageKey("c1::boss"), JSON.stringify({ version: 99 }));
    expect(loadSessionPrefs("c1::boss")).toEqual(emptySessionPrefs());
  });
});

describe("置顶 / 归档", () => {
  it("置顶可往返，且不修改原对象", () => {
    const base = emptySessionPrefs();
    const pinned = togglePin(base, "single:a");
    expect(pinned.pinned).toEqual(["single:a"]);
    expect(base.pinned).toEqual([]);
    expect(togglePin(pinned, "single:a").pinned).toEqual([]);
  });

  it("归档可往返；空 key 原样返回", () => {
    const base = emptySessionPrefs();
    const archived = toggleArchive(base, "war:b");
    expect(archived.archived).toEqual(["war:b"]);
    expect(toggleArchive(archived, "war:b").archived).toEqual([]);
    expect(toggleArchive(base, "")).toBe(base);
    expect(togglePin(base, "")).toBe(base);
  });
});

describe("markRead", () => {
  it("写入时间戳；旧时间不回退（单调）", () => {
    const base = emptySessionPrefs();
    const read = markRead(base, "single:a", 2000);
    expect(read.readAt["single:a"]).toBe(2000);
    expect(base.readAt).toEqual({});
    // 更旧的事件晚到：原样返回，不把已读改回更早
    expect(markRead(read, "single:a", 1000)).toBe(read);
    expect(markRead(read, "single:a", 3000).readAt["single:a"]).toBe(3000);
  });

  it("空 key 不产生记录", () => {
    const base = emptySessionPrefs();
    expect(markRead(base, "", 1000)).toBe(base);
  });
});

describe("unreadKeys", () => {
  it("有 updatedAt：读过之后再更新 → 未读；读完 → 已读", () => {
    const prefs = prefsWith({ readAt: { "single:a": 1000 } });
    const threads = [{ key: "single:a", updatedAt: new Date(2000).toISOString() }];
    expect(Array.from(unreadKeys(prefs, threads))).toEqual(["single:a"]);
    const readLater = prefsWith({ readAt: { "single:a": 3000 } });
    expect(Array.from(unreadKeys(readLater, threads))).toEqual([]);
  });

  it("没有 updatedAt 时退回 createdAt 比较", () => {
    const prefs = prefsWith({ readAt: { "meeting:m": 5000 } });
    const threads = [{ key: "meeting:m", createdAt: 4000 }];
    expect(Array.from(unreadKeys(prefs, threads))).toEqual([]);
    expect(Array.from(unreadKeys(prefs, [{ key: "meeting:m", createdAt: 6000 }]))).toEqual(["meeting:m"]);
  });

  it("没有时间字段：从未出现过视为未读，读过即已读", () => {
    const threads = [{ key: "single:a" }, { key: "war:b" }];
    const fresh = emptySessionPrefs();
    expect(Array.from(unreadKeys(fresh, threads)).sort()).toEqual(["single:a", "war:b"]);
    const prefs = markRead(fresh, "single:a", 1234);
    expect(Array.from(unreadKeys(prefs, threads))).toEqual(["war:b"]);
  });

  it("忽略空 key / 非法项 / 非法时间字符串", () => {
    const prefs = emptySessionPrefs();
    const threads = [
      { key: "" },
      { key: "single:a", updatedAt: "not-a-date" },
      { key: "single:b", updatedAt: "2026-01-01T00:00:00Z" },
    ];
    // single:a 的时间解析失败 → 按「从未已读」判未读
    expect(Array.from(unreadKeys(prefs, threads)).sort()).toEqual(["single:a", "single:b"]);
  });
});

describe("localStorage 隔离与写入", () => {
  it("不同 scope 互不串档，clear 只清自己的", () => {
    saveSessionPrefs("company-a::boss", prefsWith({ pinned: ["single:a"] }));
    saveSessionPrefs("company-b::boss", prefsWith({ pinned: ["single:b"] }));
    vi.advanceTimersByTime(SESSION_PREFS_DEBOUNCE_MS);

    expect(loadSessionPrefs("company-a::boss").pinned).toEqual(["single:a"]);
    expect(loadSessionPrefs("company-b::boss").pinned).toEqual(["single:b"]);

    clearSessionPrefs("company-a::boss");
    expect(loadSessionPrefs("company-a::boss")).toEqual(emptySessionPrefs());
    expect(loadSessionPrefs("company-b::boss").pinned).toEqual(["single:b"]);
  });

  it("防抖：窗口内不写，到 200ms 只落盘最后一次", () => {
    saveSessionPrefs("c1::boss", prefsWith({ pinned: ["single:a"] }));
    saveSessionPrefs("c1::boss", prefsWith({ pinned: ["single:a", "single:b"] }));
    vi.advanceTimersByTime(SESSION_PREFS_DEBOUNCE_MS - 1);
    expect(store.length).toBe(0);
    vi.advanceTimersByTime(1);
    expect(store.length).toBe(1);
    expect(loadSessionPrefs("c1::boss").pinned).toEqual(["single:a", "single:b"]);
  });

  it("clear 会丢弃还未落盘的写入", () => {
    saveSessionPrefs("c1::boss", prefsWith({ archived: ["war:b"] }));
    clearSessionPrefs("c1::boss");
    vi.advanceTimersByTime(SESSION_PREFS_DEBOUNCE_MS);
    expect(store.length).toBe(0);
    expect(loadSessionPrefs("c1::boss")).toEqual(emptySessionPrefs());
  });

  it("flushSessionPrefs 可立即落盘（卸载/切公司前用）", () => {
    saveSessionPrefs("c1::boss", prefsWith({ archived: ["war:b"] }));
    flushSessionPrefs();
    expect(loadSessionPrefs("c1::boss").archived).toEqual(["war:b"]);
    // 已落盘的 timer 不会再重复写
    vi.advanceTimersByTime(SESSION_PREFS_DEBOUNCE_MS);
    expect(store.length).toBe(1);
  });

  it("没有 window（SSR）时 save/load 都不抛错", () => {
    vi.unstubAllGlobals();
    expect(() => saveSessionPrefs("c1::boss", emptySessionPrefs())).not.toThrow();
    expect(loadSessionPrefs("c1::boss")).toEqual(emptySessionPrefs());
    flushSessionPrefs();
  });
});

describe("mergeReadAt（C3 跨设备合并）", () => {
  it("远端更新的时间戳生效，更旧的被忽略", () => {
    const base = prefsWith({ readAt: { "war:a": 100, "war:b": 500 } });
    const next = mergeReadAt(base, { "war:a": 300, "war:b": 400 });
    expect(next.readAt).toEqual({ "war:a": 300, "war:b": 500 });
  });

  it("没有任何更新时返回原对象（轮询不触发重渲染）", () => {
    const base = prefsWith({ readAt: { "war:a": 300 } });
    expect(mergeReadAt(base, { "war:a": 300 })).toBe(base);
    expect(mergeReadAt(base, { "war:a": 100 })).toBe(base);
    expect(mergeReadAt(base, {})).toBe(base);
    expect(mergeReadAt(base, null)).toBe(base);
  });

  it("非法 key / 时间戳被丢弃，不影响合法项", () => {
    const base = emptySessionPrefs();
    const next = mergeReadAt(base, {
      "": 100,
      "  ": 200,
      "war:ok": 300,
      "war:nan": Number.NaN,
      "war:neg": -5,
    } as Record<string, number>);
    expect(next.readAt).toEqual({ "war:ok": 300 });
  });

  it("纯函数：不修改传入的偏好对象", () => {
    const base = prefsWith({ readAt: { "war:a": 100 } });
    const next = mergeReadAt(base, { "war:a": 900 });
    expect(base.readAt).toEqual({ "war:a": 100 });
    expect(next).not.toBe(base);
  });
});
