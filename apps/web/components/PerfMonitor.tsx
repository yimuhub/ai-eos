"use client";

/**
 * 交互延迟采样器（工包 C7）。
 *
 * - 路由切换：从 pathname 变化那一刻计时，等两次 rAF（React 提交 + 浏览器绘制）
 *   后记为 `route-paint`，近似「用户看到新页面」的延迟；
 * - 长任务：PerformanceObserver 监听 `longtask`，把 duration 记为 `longtask`。
 *
 * 只写 sessionStorage，不发请求、不渲染任何东西；SSR 阶段无副作用。
 * 采样逻辑全部在 lib/perf.ts，这里只负责接线。
 */

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { recordInteraction } from "@/lib/perf";

export default function PerfMonitor() {
  const pathname = usePathname();
  const lastPath = useRef<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    // 首次挂载不是「路由变化」，不产生样本
    if (lastPath.current === null) {
      lastPath.current = pathname;
      return;
    }
    if (lastPath.current === pathname) return;
    lastPath.current = pathname;

    if (typeof requestAnimationFrame !== "function") return;
    const startedAt = performance.now();
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        recordInteraction("route-paint", performance.now() - startedAt);
      });
    });
    return () => {
      cancelAnimationFrame(first);
      if (second) cancelAnimationFrame(second);
    };
  }, [pathname]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (typeof PerformanceObserver === "undefined") return;
    const supported = PerformanceObserver.supportedEntryTypes;
    if (!supported || !supported.includes("longtask")) return;

    let observer: PerformanceObserver | null = null;
    try {
      observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          recordInteraction("longtask", entry.duration);
        }
      });
      observer.observe({ entryTypes: ["longtask"] });
    } catch {
      observer?.disconnect();
      return;
    }
    return () => observer?.disconnect();
  }, []);

  return null;
}
