/**
 * 工作台左栏的会话偏好：置顶 / 归档 / 已读。
 *
 * 全部存在 localStorage，**按「公司 + 账号」隔离**（scopeKey 由调用方拼好传入）。
 * 存储 key 与 payload 都带版本号：旧版本 / 损坏 JSON 一律降级为空数组，
 * 绝不让一条脏数据把左栏搞白屏。
 *
 * 这里不碰 React、不发请求：load/save 之外都是纯函数，方便 vitest 直接测。
 */

export const SESSION_PREFS_VERSION = 1;

/** 合并写入窗口：连续置顶/已读只在 200ms 后落盘一次 */
export const SESSION_PREFS_DEBOUNCE_MS = 200;

export type SessionPrefs = {
  version: 1;
  pinned: string[];
  archived: string[];
  /** 会话 key → 最近一次已读时间（毫秒时间戳） */
  readAt: Record<string, number>;
};

/** unreadKeys 只需要这几个字段，Thread 结构可直接传入 */
export type SessionThreadLike = {
  key: string;
  /** 有则按时间比较，没有则按「从未标记已读」判首次 */
  updatedAt?: string | number | null;
  createdAt?: string | number | null;
};

/** 每次返回新对象：调用方直接 setState 不会共享可变数组 */
export function emptySessionPrefs(): SessionPrefs {
  return { version: SESSION_PREFS_VERSION, pinned: [], archived: [], readAt: {} };
}

/* ---------------- 解析与清洗 ---------------- */

function normalizeKeys(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (typeof item !== "string") continue;
    const key = item.trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
  }
  return out;
}

function normalizeReadAt(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (!key.trim()) continue;
    const n = typeof raw === "number" ? raw : Number(raw);
    if (Number.isFinite(n) && n >= 0) out[key] = n;
  }
  return out;
}

/**
 * 字符串 → 偏好。任何异常（非 JSON / 非对象 / 版本不符 / 字段非法）
 * 都返回空偏好，调用方无需 try/catch。
 */
export function parseSessionPrefs(raw: unknown): SessionPrefs {
  if (typeof raw !== "string" || !raw) return emptySessionPrefs();
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return emptySessionPrefs();
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return emptySessionPrefs();
  const o = obj as Record<string, unknown>;
  // 版本不匹配直接丢弃，不做迁移（字段很少，迁移不划算）
  if (o.version !== SESSION_PREFS_VERSION) return emptySessionPrefs();
  return {
    version: SESSION_PREFS_VERSION,
    pinned: normalizeKeys(o.pinned),
    archived: normalizeKeys(o.archived),
    readAt: normalizeReadAt(o.readAt),
  };
}

/* ---------------- 纯操作（返回新对象） ---------------- */

function toggleKey(list: string[], key: string): string[] {
  return list.includes(key) ? list.filter((k) => k !== key) : [...list, key];
}

export function togglePin(prefs: SessionPrefs, key: string): SessionPrefs {
  if (!key) return prefs;
  return { ...prefs, pinned: toggleKey(prefs.pinned, key) };
}

export function toggleArchive(prefs: SessionPrefs, key: string): SessionPrefs {
  if (!key) return prefs;
  return { ...prefs, archived: toggleKey(prefs.archived, key) };
}

/**
 * 标记已读。时间戳**单调递增**：时钟回拨或旧事件晚到时直接忽略，
 * 避免「刚读完又被标未读」。
 */
export function markRead(prefs: SessionPrefs, key: string, at: number = Date.now()): SessionPrefs {
  if (!key) return prefs;
  const ts = Number.isFinite(at) ? at : Date.now();
  const prev = prefs.readAt[key];
  if (typeof prev === "number" && Number.isFinite(prev) && prev >= ts) return prefs;
  return { ...prefs, readAt: { ...prefs.readAt, [key]: ts } };
}

/**
 * 合并服务端已读位置（C3 跨设备）。
 *
 * 与本地 `markRead` 同一条规则：同一 key 取较大时间戳，绝不回退。
 * 全部不新时**返回原对象**——调用方可以直接比较引用决定是否 setState，
 * 避免 60 秒轮询每次都触发一轮重渲染。
 */
export function mergeReadAt(
  prefs: SessionPrefs,
  remote: Record<string, number> | null | undefined,
): SessionPrefs {
  if (!remote) return prefs;
  let next: Record<string, number> | null = null;
  for (const [key, raw] of Object.entries(remote)) {
    if (!key.trim()) continue;
    const ts = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(ts) || ts < 0) continue;
    const prev = prefs.readAt[key];
    if (typeof prev === "number" && Number.isFinite(prev) && prev >= ts) continue;
    if (!next) next = { ...prefs.readAt };
    next[key] = ts;
  }
  return next ? { ...prefs, readAt: next } : prefs;
}

function threadTime(thread: SessionThreadLike): number | null {
  for (const raw of [thread.updatedAt, thread.createdAt]) {
    if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) return raw;
    if (typeof raw === "string" && raw) {
      const n = Date.parse(raw);
      if (Number.isFinite(n)) return n;
    }
  }
  return null;
}

/**
 * 未读集合。
 * - 会话带 updatedAt/createdAt：读过的时间早于它 → 未读；
 * - 没有时间字段：readAt 里从未出现过 → 视为首次出现的未读。
 */
export function unreadKeys(
  prefs: SessionPrefs,
  threads: readonly SessionThreadLike[],
): Set<string> {
  const out = new Set<string>();
  for (const thread of threads) {
    if (!thread || typeof thread.key !== "string" || !thread.key) continue;
    const seen = prefs.readAt[thread.key];
    const hasSeen = typeof seen === "number" && Number.isFinite(seen);
    const ts = threadTime(thread);
    if (ts === null) {
      if (!hasSeen) out.add(thread.key);
    } else if (!hasSeen || seen < ts) {
      out.add(thread.key);
    }
  }
  return out;
}

/* ---------------- localStorage（SSR 安全 + 防抖写入） ---------------- */

export function sessionPrefsStorageKey(scopeKey: string): string {
  return `aieos_session_prefs_v${SESSION_PREFS_VERSION}:${encodeURIComponent(scopeKey || "default")}`;
}

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null; // 隐私模式 / 被禁用
  }
}

function clonePrefs(prefs: SessionPrefs): SessionPrefs {
  return {
    version: SESSION_PREFS_VERSION,
    pinned: [...prefs.pinned],
    archived: [...prefs.archived],
    readAt: { ...prefs.readAt },
  };
}

export function loadSessionPrefs(scopeKey: string): SessionPrefs {
  try {
    return parseSessionPrefs(storage()?.getItem(sessionPrefsStorageKey(scopeKey)));
  } catch {
    return emptySessionPrefs();
  }
}

const pendingWrites = new Map<string, SessionPrefs>();
let writeTimer: ReturnType<typeof setTimeout> | null = null;

/** 把待写队列立刻落盘（组件卸载、切公司前调用） */
export function flushSessionPrefs(): void {
  if (writeTimer !== null) {
    clearTimeout(writeTimer);
    writeTimer = null;
  }
  const entries = Array.from(pendingWrites.entries());
  pendingWrites.clear();
  const store = storage();
  if (!store || !entries.length) return;
  for (const [scopeKey, prefs] of entries) {
    try {
      store.setItem(sessionPrefsStorageKey(scopeKey), JSON.stringify(prefs));
    } catch {
      /* 配额满：偏好丢一次不影响使用 */
    }
  }
}

/**
 * 防抖保存：200ms 内的连续操作合并成一次写入。
 * 不同 scope 的写入各自排队，切公司不会丢上一次的改动。
 */
export function saveSessionPrefs(scopeKey: string, prefs: SessionPrefs): void {
  const scope = scopeKey || "default";
  pendingWrites.set(scope, clonePrefs(prefs));
  if (writeTimer !== null) return;
  writeTimer = setTimeout(() => {
    writeTimer = null;
    flushSessionPrefs();
  }, SESSION_PREFS_DEBOUNCE_MS);
}

/** 清空某个 scope 的偏好（同时丢弃尚未落盘的写入） */
export function clearSessionPrefs(scopeKey: string): void {
  const scope = scopeKey || "default";
  pendingWrites.delete(scope);
  try {
    storage()?.removeItem(sessionPrefsStorageKey(scope));
  } catch {
    /* 忽略 */
  }
}
