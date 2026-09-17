"use client";

// 全局轻提示（Toast）。
//
// ## 为什么需要它
// 改造前全站没有统一的失败反馈：接口 403/404/500 时，绝大多数页面
// 只是 `.catch(() => {})` 静默吞掉，或者干脆不 catch 造成
// "点了按钮没任何反应"。这是消费者最直接的不信任来源——系统不告诉你发生了什么。
//
// 现在 lib/api.ts 在任何请求失败时派发 `aieos-api-error` 事件，
// 本组件统一呈现；页面也可以主动调用 notify() 报成功/提示。
//
// 设计：右上角堆叠、成功/提示 3 秒自动消失、错误 8 秒（留出阅读时间），
// 均可手动关闭；错误带 role="alert" 以便辅助技术播报。

import { useCallback, useEffect, useRef, useState } from "react";

export type ToastKind = "success" | "error" | "info" | "warning";

export type ToastItem = {
  id: number;
  kind: ToastKind;
  message: string;
  /** 相同 key 的提示会覆盖旧的，避免轮询失败时刷屏 */
  key?: string;
};

type Listener = (item: Omit<ToastItem, "id">) => void;

const listeners = new Set<Listener>();
let seq = 0;

/** 任意模块/组件可调用：notify("success", "已保存") */
export function notify(kind: ToastKind, message: string, key?: string) {
  if (!message) return;
  listeners.forEach((fn) => fn({ kind, message, key }));
}

const AUTO_DISMISS: Record<ToastKind, number> = {
  success: 3000,
  info: 4000,
  warning: 6000,
  error: 8000,
};

const STYLES: Record<ToastKind, { border: string; bg: string; icon: string }> = {
  success: { border: "rgba(52,211,153,0.45)", bg: "rgba(52,211,153,0.12)", icon: "✅" },
  error: { border: "rgba(248,113,113,0.5)", bg: "rgba(248,113,113,0.14)", icon: "⛔" },
  warning: { border: "rgba(251,191,36,0.45)", bg: "rgba(251,191,36,0.12)", icon: "⚠️" },
  info: { border: "rgba(99,102,241,0.45)", bg: "rgba(99,102,241,0.12)", icon: "ℹ️" },
};

export default function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (item: Omit<ToastItem, "id">) => {
      const id = ++seq;
      setItems((prev) => {
        // 同 key 覆盖：轮询接口连续失败时只保留最新一条
        const next = item.key ? prev.filter((t) => t.key !== item.key) : prev;
        return [...next, { ...item, id }].slice(-5); // 最多同时 5 条
      });
      const timer = setTimeout(() => remove(id), AUTO_DISMISS[item.kind]);
      timers.current.set(id, timer);
    },
    [remove],
  );

  // 订阅通过 notify() 发起的提示
  useEffect(() => {
    const fn: Listener = (item) => push(item);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, [push]);

  // 订阅 API 层自动派发的失败事件
  useEffect(() => {
    const onApiError = (e: Event) => {
      const detail = (e as CustomEvent).detail || {};
      const msg = detail.message || "请求失败";
      push({ kind: "error", message: msg, key: `api:${detail.status}:${detail.path}` });
    };
    window.addEventListener("aieos-api-error", onApiError);
    return () => window.removeEventListener("aieos-api-error", onApiError);
  }, [push]);

  // 卸载时清理全部定时器，避免内存泄漏
  useEffect(() => {
    const map = timers.current;
    return () => {
      map.forEach((t) => clearTimeout(t));
      map.clear();
    };
  }, []);

  if (!items.length) return null;

  return (
    <div
      className="fixed top-4 right-4 z-[9999] flex flex-col gap-2 pointer-events-none"
      aria-live="polite"
      aria-atomic="false"
    >
      {items.map((t) => {
        const s = STYLES[t.kind];
        return (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            className="pointer-events-auto max-w-[420px] rounded-xl border px-3.5 py-2.5 text-sm shadow-lg backdrop-blur-md flex items-start gap-2.5 animate-fade-up"
            style={{ borderColor: s.border, background: s.bg, color: "var(--foreground)" }}
          >
            <span aria-hidden className="shrink-0 leading-5">{s.icon}</span>
            <span className="flex-1 whitespace-pre-wrap break-words leading-5">{t.message}</span>
            <button
              onClick={() => remove(t.id)}
              aria-label="关闭提示"
              className="shrink-0 text-xs opacity-50 hover:opacity-100 transition-opacity"
            >
              ✕
            </button>
          </div>
        );
      })}
    </div>
  );
}
