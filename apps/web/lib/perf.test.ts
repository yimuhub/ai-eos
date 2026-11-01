// lib/perf.ts 的测试：分位数边界 / 线性插值 / 环形缓冲上限 /
// 假 Storage 注入的持久化往返 / 非法值拒绝 / 分组统计 / 导航摘要。
// 不依赖 jsdom：Storage 全部手工注入。
import { beforeEach, describe, expect, it } from "vitest";
import {
  PERF_MAX_INTERACTION_MS,
  PERF_SAMPLES_KEY,
  PERF_SAMPLES_LIMIT,
  appendInteractionSample,
  clearInteractionSamples,
  getInteractionStats,
  parseInteractionSamples,
  percentile,
  recordInteraction,
  statsFromSamples,
  summarizeNavigation,
  type InteractionSample,
  type PerfStorage,
} from "./perf";

function fakeStorage(seed?: Record<string, string>): PerfStorage & { map: Map<string, string> } {
  const map = new Map<string, string>(Object.entries(seed ?? {}));
  return {
    map,
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, String(value));
    },
  };
}

function throwingStorage(): PerfStorage {
  return {
    getItem: () => {
      throw new Error("storage disabled");
    },
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
}

function sample(name: string, ms: number): InteractionSample {
  return { name, ms, at: 0 };
}

beforeEach(() => {
  // 内存兜底是模块级状态，测试之间必须清掉
  clearInteractionSamples(null);
});

describe("percentile", () => {
  it("空数组返回 0，单个值始终返回该值", () => {
    expect(percentile([], 50)).toBe(0);
    expect(percentile([], 95)).toBe(0);
    expect(percentile([42], 0)).toBe(42);
    expect(percentile([42], 100)).toBe(42);
    expect(percentile([42], 95)).toBe(42);
  });

  it("P0 / P50 / P95 / P100 与线性插值", () => {
    const values = [10, 20, 30, 40];
    expect(percentile(values, 0)).toBe(10);
    expect(percentile(values, 50)).toBe(25); // rank 1.5 → 20 + (30-20)*0.5
    expect(percentile(values, 100)).toBe(40);
    expect(percentile([0, 100], 95)).toBe(95);
    expect(percentile([0, 10], 25)).toBe(2.5);
  });

  it("p 超出 [0,100] 时夹取，忽略非有限值", () => {
    const values = [5, 1, 3];
    expect(percentile(values, -20)).toBe(1);
    expect(percentile(values, 200)).toBe(5);
    expect(percentile([1, Number.NaN, 3, Number.POSITIVE_INFINITY], 50)).toBe(2);
  });

  it("不修改入参数组", () => {
    const values = [3, 1, 2];
    percentile(values, 50);
    expect(values).toEqual([3, 1, 2]);
  });
});

describe("环形缓冲", () => {
  it("appendInteractionSample 只保留最后 limit 条", () => {
    let samples: InteractionSample[] = [];
    for (let i = 0; i < 5; i++) samples = appendInteractionSample(samples, sample("x", i), 3);
    expect(samples.map((s) => s.ms)).toEqual([2, 3, 4]);
  });

  it("recordInteraction 超过 200 条后丢弃最早样本", () => {
    const store = fakeStorage();
    for (let i = 0; i < PERF_SAMPLES_LIMIT + 5; i++) {
      recordInteraction("route-paint", i, store);
    }
    const stats = getInteractionStats(store);
    expect(stats).toHaveLength(1);
    expect(stats[0].count).toBe(PERF_SAMPLES_LIMIT);
    expect(stats[0].max).toBe(PERF_SAMPLES_LIMIT + 4);
    const persisted = JSON.parse(store.map.get(PERF_SAMPLES_KEY) || "{}");
    expect(persisted.version).toBe(1);
    expect(persisted.samples).toHaveLength(PERF_SAMPLES_LIMIT);
    expect(persisted.samples[0].ms).toBe(5); // 0..4 已被挤掉
  });

  it("写进带版本号的 sessionStorage key，并能从原始 JSON 再读回", () => {
    const store = fakeStorage();
    recordInteraction("longtask", 123.4, store);
    recordInteraction("longtask", 88, store);
    expect(PERF_SAMPLES_KEY).toContain("_v1");
    const raw = store.map.get(PERF_SAMPLES_KEY);
    expect(typeof raw).toBe("string");

    const reloaded = fakeStorage({ [PERF_SAMPLES_KEY]: raw as string });
    const stats = getInteractionStats(reloaded);
    expect(stats[0].count).toBe(2);
    expect(stats[0].p50).toBeCloseTo(105.7, 1);
    expect(stats[0].max).toBe(123.4);
  });
});

describe("输入校验", () => {
  it("拒绝负数 / 非有限值 / 空 name", () => {
    const store = fakeStorage();
    recordInteraction("route-paint", -1, store);
    recordInteraction("route-paint", Number.NaN, store);
    recordInteraction("route-paint", Number.POSITIVE_INFINITY, store);
    recordInteraction("", 10, store);
    recordInteraction("   ", 10, store);
    expect(getInteractionStats(store)).toEqual([]);
  });

  it("超过 10 分钟的样本被夹到上限", () => {
    const store = fakeStorage();
    recordInteraction("route-paint", 1e9, store);
    expect(getInteractionStats(store)[0].max).toBe(PERF_MAX_INTERACTION_MS);
  });

  it("损坏 JSON / 版本不符 / 非法字段都降级", () => {
    expect(parseInteractionSamples("not-json")).toEqual([]);
    expect(parseInteractionSamples(JSON.stringify({ version: 2, samples: [] }))).toEqual([]);
    expect(
      parseInteractionSamples(
        JSON.stringify({
          version: 1,
          samples: [null, { name: "", ms: 1 }, { name: "ok", ms: 5, at: 9 }, { name: "bad", ms: -2 }],
        }),
      ),
    ).toEqual([{ name: "ok", ms: 5, at: 9 }]);
  });
});

describe("存储不可用时的内存兜底", () => {
  it("显式传 null 也能记录与统计", () => {
    recordInteraction("route-paint", 12, null);
    recordInteraction("route-paint", 24, null);
    const stats = getInteractionStats(null);
    expect(stats[0].count).toBe(2);
    expect(stats[0].p50).toBe(18);
  });

  it("sessionStorage 抛异常时静默降级，不影响调用方", () => {
    const broken = throwingStorage();
    expect(() => recordInteraction("longtask", 60, broken)).not.toThrow();
    expect(getInteractionStats(broken)[0].count).toBe(1);
  });
});

describe("statsFromSamples", () => {
  it("按 name 分组统计 count / p50 / p95 / max", () => {
    const stats = statsFromSamples([
      sample("route-paint", 10),
      sample("route-paint", 20),
      sample("route-paint", 30),
      sample("longtask", 120),
    ]);
    expect(stats).toHaveLength(2);
    const route = stats.find((s) => s.name === "route-paint")!;
    expect(route.count).toBe(3);
    expect(route.p50).toBe(20);
    expect(route.p95).toBe(29); // 20 + (30-20)*0.9
    expect(route.max).toBe(30);
    const longtask = stats.find((s) => s.name === "longtask")!;
    expect(longtask.count).toBe(1);
    expect(longtask.p50).toBe(120);
    expect(longtask.p95).toBe(120);
    // 样本多的排前面
    expect(stats[0].name).toBe("route-paint");
  });

  it("空样本返回空数组", () => {
    expect(statsFromSamples([])).toEqual([]);
  });
});

describe("summarizeNavigation", () => {
  it("从 navigation entry 提取并取整", () => {
    const entries = [
      { entryType: "navigation", domContentLoadedEventEnd: 123.6, loadEventEnd: 456.2 },
    ] as unknown as PerformanceEntryList;
    expect(summarizeNavigation(entries)).toEqual({ domContentLoadedMs: 124, loadMs: 456 });
  });

  it("End 为 0 / 缺字段时退回 Start，缺失则返回 0", () => {
    const partial = [
      { entryType: "navigation", domContentLoadedEventEnd: 0, domContentLoadedEventStart: 50 },
    ] as unknown as PerformanceEntryList;
    expect(summarizeNavigation(partial)).toEqual({ domContentLoadedMs: 50, loadMs: 0 });

    const missing = [{ entryType: "navigation" }] as unknown as PerformanceEntryList;
    expect(summarizeNavigation(missing)).toEqual({ domContentLoadedMs: 0, loadMs: 0 });
  });

  it("空列表 / null / 非 navigation 条目都返回零值", () => {
    expect(summarizeNavigation([])).toEqual({ domContentLoadedMs: 0, loadMs: 0 });
    expect(summarizeNavigation(null)).toEqual({ domContentLoadedMs: 0, loadMs: 0 });
    const paint = [{ entryType: "paint", duration: 10 }] as unknown as PerformanceEntryList;
    expect(summarizeNavigation(paint)).toEqual({ domContentLoadedMs: 0, loadMs: 0 });
  });
});
