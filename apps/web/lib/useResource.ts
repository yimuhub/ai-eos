"use client";

/**
 * 极简数据层：把"列表该多新鲜"这件事收到一处统一负责。
 *
 * ## 为什么不用 SWR / React Query
 *
 * 本项目实际需要的只有三件事：并发去重、窗口聚焦重取、手动失效。
 * 为这三件事引入一个运行时依赖（连同它的体积、SSR 配置与升级成本）
 * 不划算——尤其是这个项目的目标是"克隆下来就能跑"。
 *
 * ## 它解决的具体问题
 *
 * 改造前各页面各自手写 `useEffect + setState`，于是出现两种极端：
 * 有的页面开了 5 秒轮询（一直打扰后端，且切回页面时第一眼是旧数据），
 * 有的页面只在挂载时拉一次（新建了员工，切回来还是空列表）。
 * 两种都不是用户想要的：用户想要的是**看到的时候数据是新的**。
 *
 * ## 使用
 *
 * ```tsx
 * const { data, error, loading, refresh } = useResource(
 *   cid ? `agents:${cid}` : null,
 *   () => api.get<Agent[]>(`/agents?company_id=${cid}`),
 * );
 * ```
 *
 * `key` 传 null 表示"条件未满足，先不要请求"（例如还没选公司）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getActiveCompanyId, setActiveCompanyId, getToken } from "@/lib/api";

/**
 * 激活公司自愈：校验缓存 cid 是否属于当前登录账号。
 *
 * 背景：localStorage 的 aieos_active_cid 是**跨账号**持久化的——换账号登录、
 * 或开发期用测试账号污染过缓存，都会让缓存 cid 指向"当前账号名下的不存在"，
 * 后端 owner 校验直接 404，各列表页整页报"加载失败：Company not found"。
 *
 * 缓存键 = 登录 token：同账号复用结果（防页面并发风暴），SPA 内换账号
 * 登录时 token 变化 → 自动重新校验。
 */
let ensurePromise: Promise<void> | null = null;
let ensureToken: string | null | undefined = undefined;
export function ensureActiveCompanyId(): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  const token = getToken() || "";
  if (ensurePromise && ensureToken === token) return ensurePromise;
  ensureToken = token;
  ensurePromise = (async () => {
    const current = getActiveCompanyId();
    try {
      const { api } = await import("@/lib/api");
      const companies = await api.get<{ id: string }[]>("/companies");
      const list = companies || [];
      const valid = !!current && list.some((c) => c.id === current);
      if (!valid) {
        const fallback = list[0]?.id;
        if (fallback) {
          setActiveCompanyId(fallback); // 内部派发 company-changed，各页自动重取
        } else {
          localStorage.removeItem("aieos_active_cid");
          window.dispatchEvent(new CustomEvent("aieos-company-changed", { detail: null }));
          // 名下无公司 → 各页走"该走引导流程"（useActiveCompanyId 返回 null）
        }
      }
    } catch {
      // 校验请求本身失败（未登录 / 网络）→ 不动原流程，避免误清
    }
  })();
  return ensurePromise;
}

interface CacheEntry {
  data: unknown;
  at: number;
}

const cache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<unknown>>();
const listeners = new Map<string, Set<() => void>>();
// 每个 key 的请求代号：强制刷新会发出新请求，旧请求晚到时必须知道
// "自己已经过时"，否则会把新结果覆盖回旧数据。
const attempts = new Map<string, number>();

function notify(key: string) {
  listeners.get(key)?.forEach((fn) => fn());
}

/** 让某个 key（或某前缀下的所有 key）失效，下次使用时重新拉取。 */
export function invalidate(prefix?: string) {
  if (!prefix) {
    cache.clear();
    for (const key of Array.from(listeners.keys())) notify(key);
    return;
  }
  // 用 Array.from 快照键集合：notify 的回调里可能会增删缓存，
  // 直接迭代 Map 视图会踩到"迭代中被修改"的不确定行为。
  for (const key of Array.from(cache.keys())) {
    if (key.startsWith(prefix)) {
      cache.delete(key);
      notify(key);
    }
  }
}

/** 直接写入缓存（用于本地乐观更新，避免整页重新拉取）。 */
export function setCached<T>(key: string, data: T) {
  cache.set(key, { data, at: Date.now() });
  notify(key);
}

function isLatestAttempt(key: string, attempt: number) {
  return attempts.get(key) === attempt;
}

/**
 * 返回请求本身与它的代号。强制刷新（force）不复用旧的在途请求：
 * 否则"挂载时的 GET 还没回来、紧接着的写操作 refresh()"会并进那次旧请求，
 * 页面拿到的仍是写之前的快照——这就是强制刷新失效的根因。
 */
function load<T>(
  key: string,
  fetcher: () => Promise<T>,
  force = false,
): { promise: Promise<T>; attempt: number } {
  const existing = inflight.get(key);
  // 并发去重：同一 key 的多个组件同时挂载时只发一次请求；
  // 强制刷新则必须另起一次，并顶掉旧的在途条目。
  if (existing && !force) {
    return { promise: existing as Promise<T>, attempt: attempts.get(key) ?? 0 };
  }

  const attempt = (attempts.get(key) ?? 0) + 1;
  attempts.set(key, attempt);

  const p = fetcher()
    .then((data) => {
      // 旧请求晚到：不能写缓存（会覆盖新结果），也不能删 inflight（那是新请求的）
      if (isLatestAttempt(key, attempt)) {
        cache.set(key, { data, at: Date.now() });
        inflight.delete(key);
        notify(key);
      }
      return data;
    })
    .catch((err) => {
      if (isLatestAttempt(key, attempt)) inflight.delete(key);
      throw err;
    });
  inflight.set(key, p);
  return { promise: p, attempt };
}

export interface ResourceOptions {
  /** 超过这个时长后，窗口重新获得焦点会重新拉取。默认 30 秒。 */
  staleMs?: number;
  revalidateOnFocus?: boolean;
}

export interface ResourceResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
  mutate: (next: T) => void;
}

export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  options: ResourceOptions = {},
): ResourceResult<T> {
  const { staleMs = 30_000, revalidateOnFocus = true } = options;

  // fetcher 通常是内联箭头函数，每次渲染都是新引用；
  // 用 ref 持有它，effect 只依赖 key，避免无限循环。
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const cached = key ? (cache.get(key)?.data as T | undefined) : undefined;
  const [data, setData] = useState<T | null>(cached ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(key ? cached === undefined : false);
  const aliveRef = useRef(true);
  // 代际计数：key 切换（如换公司 agents:${cid} → agents:${cid2}）时，
  // 旧 key 仍在途的请求若只靠 aliveRef（会被新 effect 重置为 true）判断，
  // 就会把旧公司的数据 setData 进现在跟踪新 key 的组件 → 页面显示旧公司数据。
  const genRef = useRef(0);

  const run = useCallback(
    async (force: boolean, gen: number = genRef.current) => {
      if (!key) return;
      const entry = cache.get(key);
      if (!force && entry) {
        setData(entry.data as T);
        setLoading(false);
        return;
      }
      setLoading(true);
      const { promise, attempt } = load(key, () => fetcherRef.current(), force);
      try {
        const next = await promise;
        if (!aliveRef.current || gen !== genRef.current) return;
        // 期间若已有更新请求发出（如写操作后的 refresh），本次结果已过时，
        // 不能再 setData——否则旧快照会在新数据之后渲染出来。
        if (!isLatestAttempt(key, attempt)) return;
        setData(next);
        setError(null);
      } catch (e) {
        if (!aliveRef.current || gen !== genRef.current) return;
        if (!isLatestAttempt(key, attempt)) return;
        setError(e instanceof Error ? e.message : "加载失败");
      } finally {
        if (aliveRef.current && gen === genRef.current && isLatestAttempt(key, attempt)) {
          setLoading(false);
        }
      }
    },
    [key],
  );

  useEffect(() => {
    const gen = ++genRef.current;
    aliveRef.current = true;
    // 错误是 key 作用域的状态：切 key（换公司 / 换视图）后旧 key 的失败原因
    // 与当前内容无关，先清掉，避免新 key 还没回来就显示上一屏的错误。
    setError(null);
    if (!key) {
      setData(null);
      setLoading(false);
      return () => {
        aliveRef.current = false;
      };
    }
    // 有缓存先立刻渲染，再按新鲜度决定是否后台重取：
    // 用户看到的是内容而不是骨架屏，同时数据仍会更新。
    const entry = cache.get(key);
    if (entry) {
      setData(entry.data as T);
      setLoading(false);
      if (Date.now() - entry.at > staleMs) void run(true, gen);
    } else {
      void run(false, gen);
    }

    const listener = () => {
      const fresh = cache.get(key);
      if (fresh) setData(fresh.data as T);
    };
    const set = listeners.get(key) ?? new Set();
    set.add(listener);
    listeners.set(key, set);

    return () => {
      aliveRef.current = false;
      set.delete(listener);
      if (set.size === 0) listeners.delete(key);
    };
  }, [key, run, staleMs]);

  useEffect(() => {
    if (!revalidateOnFocus || !key) return;
    const onFocus = () => {
      const entry = cache.get(key);
      if (!entry || Date.now() - entry.at > staleMs) void run(true);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [key, revalidateOnFocus, run, staleMs]);

  const refresh = useCallback(() => void run(true), [run]);
  const mutate = useCallback(
    (next: T) => {
      if (key) setCached(key, next);
      setData(next);
    },
    [key],
  );

  return { data, error, loading, refresh, mutate };
}

/**
 * 当前激活公司，且**会跟随切换而变化**。
 *
 * 改造前各页面写的是 `const cs = await api.get("/companies"); const cid = cs[0].id;`
 * ——看起来没问题，但那是一次性的快照：用户在顶栏切了公司之后，
 * 只有回到首页重新挂载才会生效，其他页面继续显示旧公司的数据。
 * 这属于"界面在说谎"：公司名换了，列表没换。
 *
 * 现在把它做成一个可订阅的值：切公司 → 所有用了它的页面自动重取。
 *
 * 返回 `undefined` 表示"还没读出来"（首帧、SSR），`null` 表示"确实没有激活公司"。
 * 两者要分开，否则页面无法区分"加载中"和"该走引导流程"。
 */
export function useActiveCompanyId(): string | null | undefined {
  const [cid, setCid] = useState<string | null | undefined>(undefined);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // 不再立即出缓存值：先等 ensureActiveCompanyId 完成归属校验/纠正，
    // 再读纠正后的 localStorage。这样"跨账号污染 cid → 404"的窗口彻底消失。
    // ensureActiveCompanyId 是模块级单例（全应用只发一次 /companies），
    // 首个页面等一次往返，后续页面几乎立即 resolve。
    ensureActiveCompanyId().then(() => {
      setCid(getActiveCompanyId());
      setReady(true);
    });
    const onChange = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail;
      setCid(detail || getActiveCompanyId());
    };
    // localStorage 写入本身不触发事件（只有同页 setActiveCompanyId 会派发），
    // 所以额外监听 storage —— 覆盖"另一个标签页切了公司"的情况。
    const onStorage = (e: StorageEvent) => {
      if (e.key === "aieos_active_cid") setCid(e.newValue);
    };
    window.addEventListener("aieos-company-changed", onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("aieos-company-changed", onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // 校验完成前一律返回 undefined（= "还在确认用哪家公司，先别发请求"），
  // 避免用未校验的缓存 cid 打出 404。
  return ready ? cid : undefined;
}

/**
 * 页面隐藏时暂停轮询，可见时立即补一次。
 *
 * 首页指挥中心需要 5 秒一次的新鲜度（任务正在跑），但**后台标签页不该继续打后端**：
 * 用户看不见的刷新是纯粹的成本，还可能与真正的前台请求抢连接。
 */
export function useVisibilityInterval(fn: () => void, ms: number) {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer) return;
      timer = setInterval(() => fnRef.current(), ms);
    };
    const stop = () => {
      if (timer) { clearInterval(timer); timer = null; }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        fnRef.current(); // 回到前台先补一次，避免第一眼看到旧数据
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [ms]);
}
