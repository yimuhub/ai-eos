// 会话已读跨设备同步（C3）前端数据层。
//
// 本地已读位置仍然以 localStorage 为准（离线可用、首屏无等待），
// 这里只做两件补充：
//  * 上报：打开会话时把 key + 毫秒时间戳排队，防抖后批量 POST；
//  * 拉取：挂载 / 回到前台 / 每 60s 拉一次，按 max 合并进本地偏好。
//
// 失败姿态：读/写都走 `silent`，失败回填到队列下次再试。已读同步丢了
// 最多多一个未读点，不值得打扰用户。

import { api, getActiveCompanyId } from "./api";

export type ThreadRead = { thread_key: string; read_at: number };

/** 与后端 MAX_BATCH 对齐：单次最多 200 条，超出丢最旧的。 */
export const THREAD_READS_MAX_BATCH = 200;
/** 连续点开多个会话时合并成一次请求 */
export const THREAD_READS_DEBOUNCE_MS = 800;
/** 跨设备收敛周期：已读不是秒级业务，60s 足够且不吵 */
export const THREAD_READS_POLL_INTERVAL_MS = 60_000;

function normalizeTs(ts: number): number | null {
  if (!Number.isFinite(ts)) return null;
  const n = Math.floor(ts);
  return n >= 0 ? n : null;
}

/** 服务端行 → `readAt` 映射；非法行直接丢弃。 */
export function readsToMap(rows: readonly ThreadRead[] | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  if (!Array.isArray(rows)) return out;
  for (const row of rows) {
    if (!row || typeof row.thread_key !== "string") continue;
    const key = row.thread_key.trim();
    const ts = normalizeTs(row.read_at);
    if (!key || ts === null) continue;
    if ((out[key] ?? -1) < ts) out[key] = ts;
  }
  return out;
}

export async function fetchThreadReads(companyId?: string | null): Promise<ThreadRead[]> {
  const cid = companyId || getActiveCompanyId();
  if (!cid) return [];
  const data = await api.get<{ reads?: ThreadRead[] }>(
    `/thread-reads?company_id=${encodeURIComponent(cid)}`,
    { silent: true },
  );
  return Array.isArray(data?.reads) ? data.reads : [];
}

/* ---------------- 上报队列（公司 → key → 时间戳） ---------------- */

const queues = new Map<string, Map<string, number>>();
let timer: ReturnType<typeof setTimeout> | null = null;

function prune(q: Map<string, number>): void {
  while (q.size > THREAD_READS_MAX_BATCH) {
    let oldestKey: string | null = null;
    let oldest = Infinity;
    for (const [key, ts] of Array.from(q)) {
      if (ts < oldest) {
        oldest = ts;
        oldestKey = key;
      }
    }
    if (oldestKey === null) break;
    q.delete(oldestKey);
  }
}

/** 排队一条已读；同一 key 只增不减，与后端合并规则一致。 */
export function queueThreadRead(companyId: string, threadKey: string, readAt: number): void {
  const cid = (companyId || "").trim();
  const key = (threadKey || "").trim();
  if (!cid || !key) return;
  const ts = normalizeTs(readAt);
  if (ts === null) return;

  const q = queues.get(cid) ?? new Map<string, number>();
  const prev = q.get(key);
  if (typeof prev === "number" && prev >= ts) return;
  q.set(key, ts);
  prune(q);
  queues.set(cid, q);

  if (timer === null && typeof setTimeout === "function") {
    timer = setTimeout(() => {
      timer = null;
      void flushThreadReads();
    }, THREAD_READS_DEBOUNCE_MS);
  }
}

/** 立刻发送队列；失败的条目回填，等下一次触发。 */
export async function flushThreadReads(): Promise<void> {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
  const entries = Array.from(queues.entries());
  queues.clear();
  await Promise.all(entries.map(async ([cid, q]) => {
    const reads = Array.from(q, ([thread_key, read_at]) => ({ thread_key, read_at }));
    try {
      await api.post("/thread-reads", { company_id: cid, reads }, { silent: true });
    } catch {
      const back = queues.get(cid) ?? new Map<string, number>();
      for (const [key, ts] of Array.from(q)) {
        if ((back.get(key) ?? -1) < ts) back.set(key, ts);
      }
      prune(back);
      queues.set(cid, back);
    }
  }));
}

/** 测试用：清空队列并取消待发送定时器。 */
export function resetThreadReads(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
  queues.clear();
}

/**
 * 轮询远端已读并回调合并结果。
 *
 * - 页面不可见时暂停，回到前台立即补一次；
 * - 返回停止函数，组件卸载时调用并 `flushThreadReads()` 收尾。
 */
export function startThreadReadPolling(
  onRemote: (rows: ThreadRead[]) => void,
  companyId?: string | null,
  intervalMs: number = THREAD_READS_POLL_INTERVAL_MS,
): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return () => {};
  }
  const cid = companyId || getActiveCompanyId();
  if (!cid) return () => {};
  const every = Math.max(intervalMs, 15_000);
  let stopTimer: (() => void) | null = null;
  let stopped = false;

  const pull = async () => {
    if (stopped) return;
    try {
      const rows = await fetchThreadReads(cid);
      if (!stopped && rows.length) onRemote(rows);
    } catch {
      /* 静默：网络抖动不该弹全局错误 */
    }
  };
  const start = () => {
    if (stopTimer) return;
    const id = setInterval(pull, every);
    stopTimer = () => clearInterval(id);
  };
  const stop = () => {
    stopTimer?.();
    stopTimer = null;
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") {
      void pull();
      start();
    } else {
      stop();
      void flushThreadReads();
    }
  };
  const onPageHide = () => { void flushThreadReads(); };

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide);
  void pull();
  if (document.visibilityState === "visible") start();

  return () => {
    stopped = true;
    stop();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", onPageHide);
  };
}
