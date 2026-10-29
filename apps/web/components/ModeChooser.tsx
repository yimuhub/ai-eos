"use client";

// 首次进入系统时必须二选一，之后随时可以从 ⌘K 或侧栏底部再打开。

import { useCallback, useEffect, useState } from "react";
import { useSwitchMode, useUIMode, type UIMode } from "@/lib/ui-mode";

let opener: (() => void) | null = null;

/** 打开切换弹窗（供 ⌘K 动作、两侧栏底部按钮调用）。 */
export function openModeChooser() {
  opener?.();
}

export default function ModeChooser() {
  const mode = useUIMode();
  const switchMode = useSwitchMode();
  const forced = mode === null;
  const [open, setOpen] = useState(false);

  useEffect(() => {
    opener = () => setOpen(true);
    return () => {
      opener = null;
    };
  }, []);

  useEffect(() => {
    if (forced) setOpen(true);
  }, [forced]);

  // 强制选择时 Esc 不生效——没有模式就没有界面可回退
  useEffect(() => {
    if (!open || forced) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, forced]);

  const choose = useCallback((m: UIMode) => {
    switchMode(m);
    setOpen(false);
  }, [switchMode]);

  if (!open && !forced) return null;

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center px-4"
      style={{ background: "rgba(4,4,8,0.72)", backdropFilter: "blur(4px)" }}
      onMouseDown={(e) => {
        if (!forced && e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="mode-title"
        className="w-full max-w-2xl rounded-2xl border shadow-2xl p-6 animate-fade-up"
        style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}
      >
        <h2 id="mode-title" className="text-base font-semibold">选择界面模式</h2>
        <p className="text-xs mt-1.5" style={{ color: "var(--muted)" }}>
          两种模式功能完全相同，只是使用方式不同。之后可以在 ⌘K 或侧边栏底部随时切换。
        </p>

        <div className="grid gap-3 mt-5 sm:grid-cols-2">
          <button
            type="button"
            onClick={() => choose("classic")}
            aria-current={mode === "classic" ? "true" : undefined}
            className="group text-left rounded-xl border p-4 transition-colors hover:border-indigo-400/60"
            style={{
              borderColor: mode === "classic" ? "rgba(99,102,241,0.55)" : "var(--border)",
              background: mode === "classic" ? "rgba(99,102,241,0.08)" : "rgba(255,255,255,0.02)",
            }}
          >
            <ClassicPreview />
            <div className="flex items-center gap-2 mt-3">
              <span className="text-sm font-medium">经典模式</span>
              {mode === "classic" && <Badge>当前</Badge>}
            </div>
            <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
              固定左侧菜单 + 整页导航。点一个功能就进入那个页面，适合习惯后台系统的用法。
            </p>
          </button>

          <button
            type="button"
            onClick={() => choose("geek")}
            aria-current={mode === "geek" ? "true" : undefined}
            className="group text-left rounded-xl border p-4 transition-colors hover:border-indigo-400/60"
            style={{
              borderColor: mode === "geek" ? "rgba(99,102,241,0.55)" : "var(--border)",
              background: mode === "geek" ? "rgba(99,102,241,0.08)" : "rgba(255,255,255,0.02)",
            }}
          >
            <GeekPreview />
            <div className="flex items-center gap-2 mt-3">
              <span className="text-sm font-medium">极客模式</span>
              {mode === "geek" && <Badge>当前</Badge>}
              {mode === null && <Badge>推荐</Badge>}
            </div>
            <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
              左中右三栏对话。功能页面开在右栏标签里，聊天不中断，开多少页都留在同一张屏上。
            </p>
          </button>
        </div>

        {!forced && (
          <div className="flex justify-end mt-5">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-xs px-3 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}
            >
              取消
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[9px] px-1.5 py-px rounded-full"
      style={{ background: "rgba(99,102,241,0.2)", color: "#c7c8ff", border: "1px solid rgba(99,102,241,0.35)" }}>
      {children}
    </span>
  );
}

/** 经典：左侧固定菜单 + 一整块内容。 */
function ClassicPreview() {
  return (
    <div className="h-24 rounded-lg overflow-hidden flex" style={{ background: "#0b0b12", border: "1px solid var(--border)" }}>
      <div className="w-1/4 p-1.5 space-y-1" style={{ background: "rgba(99,102,241,0.10)", borderRight: "1px solid var(--border)" }}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-1.5 rounded-full" style={{ background: i === 0 ? "rgba(139,139,255,0.7)" : "rgba(255,255,255,0.12)" }} />
        ))}
      </div>
      <div className="flex-1 p-2 space-y-1.5">
        <div className="h-2 w-1/3 rounded" style={{ background: "rgba(255,255,255,0.22)" }} />
        <div className="h-1.5 w-3/4 rounded" style={{ background: "rgba(255,255,255,0.10)" }} />
        <div className="h-1.5 w-2/3 rounded" style={{ background: "rgba(255,255,255,0.10)" }} />
        <div className="h-1.5 w-1/2 rounded" style={{ background: "rgba(255,255,255,0.10)" }} />
      </div>
    </div>
  );
}

/** 极客：左中右三栏 + 右栏标签。 */
function GeekPreview() {
  return (
    <div className="h-24 rounded-lg overflow-hidden flex" style={{ background: "#0b0b12", border: "1px solid var(--border)" }}>
      <div className="w-1/5 p-1.5 space-y-1" style={{ background: "rgba(255,255,255,0.03)", borderRight: "1px solid var(--border)" }}>
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-1.5 rounded-full" style={{ background: "rgba(255,255,255,0.12)" }} />
        ))}
      </div>
      <div className="flex-1 p-2 space-y-1.5" style={{ borderRight: "1px solid var(--border)" }}>
        <div className="h-1.5 w-1/2 rounded-full ml-auto" style={{ background: "rgba(99,102,241,0.55)" }} />
        <div className="h-1.5 w-2/3 rounded-full" style={{ background: "rgba(255,255,255,0.12)" }} />
        <div className="h-1.5 w-1/2 rounded-full ml-auto" style={{ background: "rgba(99,102,241,0.55)" }} />
        <div className="h-1.5 w-3/5 rounded-full" style={{ background: "rgba(255,255,255,0.12)" }} />
      </div>
      <div className="w-1/3 p-1.5">
        <div className="flex gap-1 mb-1.5">
          <div className="h-1.5 w-1/3 rounded-full" style={{ background: "rgba(99,102,241,0.6)" }} />
          <div className="h-1.5 w-1/3 rounded-full" style={{ background: "rgba(255,255,255,0.18)" }} />
        </div>
        <div className="h-10 rounded" style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)" }} />
      </div>
    </div>
  );
}
