/**
 * 前端交互延迟采样（工包 C7 的可测量部分）。
 *
 * 只做「本会话」的轻量统计：路由绘制耗时 + 长任务耗时，最多保留 200 条，
 * 落在 sessionStorage（带版本号），关掉标签页就清空。不采集身份信息、不上报。
 *
 * 约定：
 * - 本模块在 import 时**不碰 window**，SSR / 构建期直接 import 也安全；
 * - 存储可注入（第三/第二参数），单测用假 Storage 即可，不依赖 jsdom；
 * - 存储不可用（隐私模式 / 配额满）时静默降级到内存，绝不打断业务；
 * - percentile / 分组统计 / 导航摘要都是纯函数，便于直接测。
 */

export const PERF_SAMPLES_VERSION = 1;
export const PERF_SAMPLES_LIMIT = 200;
/** 单条样本上限：10 分钟，避免异常时钟或脏数据把统计拉爆 */
export const PERF_MAX_INTERACTION_MS = 600_000;
/** 版本化 key：结构变了就换 key，旧数据自然失效 */
export const PERF_SAMPLES_KEY = `aieos_perf_samples_v${PERF_SAMPLES_VERSION}`;

export type InteractionSample = {
  name: string;
  ms: number;
  /** 采样时间戳（毫秒），仅用于排查顺序 */
  at: number;
};

export type InteractionStat = {
  name: string;
  count: number;
  p50: number;
  p95: number;
  max: number;
};

export type NavigationSummary = {
  domContentLoadedMs: number;
  loadMs: number;
};

/** Storage 的最小接口：测试注入假对象即可，不要求完整实现 */
export type PerfStorage = Pick<Storage, "getItem" | "setItem">;

/* ---------------- 纯函数 ---------------- */

function finiteValues(values: readonly number[]): number[] {
  return values.filter((v) => typeof v === "number" && Number.isFinite(v));
}

/**
 * 线性插值分位数（与常见 APM 口径一致）。
 * - 空数组 / 全部非法 → 0
 * - p 会被夹到 [0,100]：0 → 最小值，100 → 最大值
 */
export function percentile(values: number[], p: number): number {
  const nums = finiteValues(values);
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const clamped = Number.isFinite(p) ? Math.min(100, Math.max(0, p)) : 0;
  if (sorted.length === 1) return sorted[0];
  const rank = (clamped / 100) * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) return sorted[lower];
  const weight = rank - lower;
  return sorted[lower] + (sorted[upper] - sorted[lower]) * weight;
}

function roundMs(value: number): number {
  return Math.round(value * 10) / 10;
}

function normalizeName(name: unknown): string {
  if (typeof name !== "string") return "";
  return name.trim().slice(0, 64);
}

function sanitizeSample(raw: unknown): InteractionSample | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const name = normalizeName(o.name);
  if (!name) return null;
  const ms = typeof o.ms === "number" ? o.ms : Number(o.ms);
  if (!Number.isFinite(ms) || ms < 0) return null;
  const at = typeof o.at === "number" && Number.isFinite(o.at) ? o.at : 0;
  return { name, ms: roundMs(Math.min(ms, PERF_MAX_INTERACTION_MS)), at };
}

/** 解析存储里的 JSON；版本不符 / 损坏 / 字段非法一律降级为空数组 */
export function parseInteractionSamples(raw: unknown): InteractionSample[] {
  if (typeof raw !== "string" || !raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  const o = parsed as Record<string, unknown>;
  if (o.version !== PERF_SAMPLES_VERSION || !Array.isArray(o.samples)) return [];
  const out: InteractionSample[] = [];
  for (const item of o.samples) {
    const sample = sanitizeSample(item);
    if (sample) out.push(sample);
  }
  return out.slice(-PERF_SAMPLES_LIMIT);
}

/** 环形缓冲：追加后只保留最后 limit 条（默认 200） */
export function appendInteractionSample(
  samples: readonly InteractionSample[],
  sample: InteractionSample,
  limit: number = PERF_SAMPLES_LIMIT,
): InteractionSample[] {
  const cap = Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : PERF_SAMPLES_LIMIT;
  const next = [...samples, sample];
  return next.length > cap ? next.slice(next.length - cap) : next;
}

/** 按 name 分组统计 P50 / P95 / 最大值；样本多的排前面 */
export function statsFromSamples(samples: readonly InteractionSample[]): InteractionStat[] {
  const groups = new Map<string, number[]>();
  for (const sample of samples) {
    if (!sample || !sample.name) continue;
    if (!Number.isFinite(sample.ms) || sample.ms < 0) continue;
    const list = groups.get(sample.name);
    if (list) list.push(sample.ms);
    else groups.set(sample.name, [sample.ms]);
  }
  const out: InteractionStat[] = [];
  groups.forEach((values, name) => {
    out.push({
      name,
      count: values.length,
      p50: roundMs(percentile(values, 50)),
      p95: roundMs(percentile(values, 95)),
      max: roundMs(Math.max(...values)),
    });
  });
  out.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  return out;
}

/**
 * 从 PerformanceNavigationTiming 提取耗时。
 * 页面还没触发 load 时 End 为 0，这里退回 Start 再退回 0，绝不返回 NaN。
 */
export function summarizeNavigation(
  entries: PerformanceEntryList | null | undefined,
): NavigationSummary {
  const empty: NavigationSummary = { domContentLoadedMs: 0, loadMs: 0 };
  if (!entries || entries.length === 0) return empty;
  let nav: PerformanceEntry | null = null;
  for (const entry of Array.from(entries)) {
    if (entry && entry.entryType === "navigation") {
      nav = entry;
      break;
    }
  }
  if (!nav) return empty;

  const pick = (...keys: string[]): number => {
    const record = nav as unknown as Record<string, unknown>;
    for (const key of keys) {
      const value = record[key];
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        return Math.round(value);
      }
    }
    return 0;
  };

  return {
    domContentLoadedMs: pick("domContentLoadedEventEnd", "domContentLoadedEventStart"),
    loadMs: pick("loadEventEnd", "loadEventStart"),
  };
}

/* ---------------- 存储（可注入 + 内存兜底） ---------------- */

/** 默认存储：只在调用时读取 window，import 阶段不执行 */
function resolveStorage(storage?: PerfStorage | null): PerfStorage | null {
  if (storage !== undefined) return storage;
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage;
  } catch {
    return null; // 隐私模式 / 被策略禁用
  }
}

/** 存储不可用时的兜底；不参与跨标签页共享 */
let memorySamples: InteractionSample[] = [];

function readSamples(store: PerfStorage | null): InteractionSample[] {
  if (!store) return [...memorySamples];
  try {
    return parseInteractionSamples(store.getItem(PERF_SAMPLES_KEY));
  } catch {
    return [...memorySamples];
  }
}

function writeSamples(store: PerfStorage | null, samples: InteractionSample[]): void {
  memorySamples = samples;
  if (!store) return;
  try {
    store.setItem(
      PERF_SAMPLES_KEY,
      JSON.stringify({ version: PERF_SAMPLES_VERSION, samples }),
    );
  } catch {
    /* 配额满 / 禁写：内存里已有一份，本次会话统计不受影响 */
  }
}

/** 记录一条交互耗时；非法输入（负数 / NaN / Infinity / 空 name）直接忽略 */
export function recordInteraction(
  name: string,
  ms: number,
  storage?: PerfStorage | null,
): void {
  const label = normalizeName(name);
  if (!label) return;
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return;
  const sample: InteractionSample = {
    name: label,
    ms: roundMs(Math.min(ms, PERF_MAX_INTERACTION_MS)),
    at: Date.now(),
  };
  const store = resolveStorage(storage);
  writeSamples(store, appendInteractionSample(readSamples(store), sample));
}

/** 读取本会话统计；无样本时返回空数组 */
export function getInteractionStats(storage?: PerfStorage | null): InteractionStat[] {
  return statsFromSamples(readSamples(resolveStorage(storage)));
}

/** 清空样本（诊断页重置 / 测试隔离用） */
export function clearInteractionSamples(storage?: PerfStorage | null): void {
  memorySamples = [];
  const store = resolveStorage(storage);
  if (!store) return;
  try {
    store.setItem(PERF_SAMPLES_KEY, JSON.stringify({ version: PERF_SAMPLES_VERSION, samples: [] }));
  } catch {
    /* 忽略 */
  }
}
