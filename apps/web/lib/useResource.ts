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
import { getActiveCompanyId } from "@/lib/api";

interface CacheEntry {
  data: unknown;
  at: number;
}

const cache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<unknown>>();
const listeners = new Map<string, Set<() => void>>();

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

function load<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const existing = inflight.get(key);
  // 并发去重：同一 key 的多个组件同时挂载时只发一次请求
  if (existing) return existing as Promise<T>;

  const p = fetcher()
    .then((data) => {
      cache.set(key, { data, at: Date.now() });
      inflight.delete(key);
      notify(key);
      return data;
    })
    .catch((err) => {
      inflight.delete(key);
      throw err;
    });
  inflight.set(key, p);
  return p;
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

  const run = useCallback(
    async (force: boolean) => {
      if (!key) return;
      const entry = cache.get(key);
      if (!force && entry) {
        setData(entry.data as T);
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const next = await load(key, () => fetcherRef.current());
        if (!aliveRef.current) return;
        setData(next);
        setError(null);
      } catch (e) {
        if (!aliveRef.current) return;
        setError(e instanceof Error ? e.message : "加载失败");
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    },
    [key],
  );

  useEffect(() => {
    aliveRef.current = true;
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
      if (Date.now() - entry.at > staleMs) void run(true);
    } else {
      void run(false);
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

  useEffect(() => {
    setCid(getActiveCompanyId());
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

  return cid;
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
