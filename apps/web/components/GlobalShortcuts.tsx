"use client";

// 全局快捷键监听 + 「?」快捷键帮助面板。
//
// 挂在 (main)/layout.tsx 里，与 <CommandPalette /> 相邻——
// 两者都是"全局键盘入口"，放一起后面维护时找得到。
//
// 生效条件（缺一不可）：
//   1. 没有按住 ⌘/Ctrl/Alt（组合键留给浏览器与命令面板）；
//   2. 焦点不在输入框/文本域/下拉/富文本里（在打字时按 n 是在输入）；
//   3. 命令面板没开着（面板开着时所有按键都归它，避免抢键）。

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { SHORTCUTS, SHORTCUT_EVENTS, emitShortcut } from "@/lib/shortcuts";

function isEditable(el: EventTarget | null): boolean {
  const t = el as HTMLElement | null;
  if (!t || !(t instanceof HTMLElement)) return false;
  const tag = t.tagName;
  return (
    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
    t.isContentEditable
  );
}

/** 命令面板是否正开着：开着时所有按键都让给它 */
function isPaletteOpen(): boolean {
  if (typeof document === "undefined") return false;
  return !!document.querySelector('[aria-label="全局搜索与跳转"]');
}

/** 当前页面有没有任务列表（空格/回车只在列表页生效，避免劫持全站滚动） */
function hasTaskList(): boolean {
  if (typeof document === "undefined") return false;
  return !!document.querySelector("[data-task-list]");
}

/** 帮助面板里的 Tab / Shift+Tab 循环，不跑回页面其余控件。 */
function trapTabKey(event: React.KeyboardEvent<HTMLDivElement>, container: HTMLElement | null) {
  if (!container || event.key !== "Tab") return;
  const nodes = Array.from(container.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ));
  if (!nodes.length) {
    event.preventDefault();
    return;
  }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (!active || !container.contains(active)) {
    event.preventDefault();
    first.focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

export default function GlobalShortcuts() {
  const [helpOpen, setHelpOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  /** 打开帮助前焦点在哪：关闭时还回去 */
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => { setMounted(true); }, []);

  const openHelp = useCallback(() => {
    if (typeof document !== "undefined") {
      const active = document.activeElement;
      returnFocusRef.current = active instanceof HTMLElement ? active : null;
    }
    setHelpOpen(true);
  }, []);

  const closeHelp = useCallback(() => setHelpOpen(false), []);

  const toggleHelp = useCallback(() => {
    if (helpOpen) closeHelp();
    else openHelp();
  }, [helpOpen, openHelp, closeHelp]);

  // 打开时锁背景滚动 + 焦点进关闭按钮；关闭/卸载恢复
  useEffect(() => {
    if (!helpOpen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const timer = window.setTimeout(() => closeBtnRef.current?.focus(), 20);
    return () => {
      window.clearTimeout(timer);
      document.body.style.overflow = previous;
    };
  }, [helpOpen]);

  // 关闭时焦点回到打开前的元素
  useEffect(() => {
    if (helpOpen) return;
    const target = returnFocusRef.current;
    returnFocusRef.current = null;
    if (!target || !target.isConnected) return;
    const timer = window.setTimeout(() => {
      if (target.isConnected) target.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [helpOpen]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 修饰键组合一律放行：⌘K 归命令面板，其它组合归浏览器
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isPaletteOpen()) return;

      // Esc：只负责关帮助面板，不抢其它弹窗的 Esc
      if (e.key === "Escape") {
        if (helpOpen) { e.preventDefault(); closeHelp(); }
        return;
      }

      const editable = isEditable(e.target);

      // 「?」在输入框里也要能开帮助——这是唯一的例外，且它不产生副作用
      if (e.key === "?") {
        e.preventDefault();
        toggleHelp();
        return;
      }
      // 帮助面板开着时，其余快捷键一律让位，避免在"看说明"时误触后台
      if (helpOpen) return;
      if (editable) return;

      switch (e.key) {
        case "n":
          emitShortcut(SHORTCUT_EVENTS.newTask);
          break;
        case "/":
          // 浏览器里 "/" 没有默认行为，但保险起见仍 preventDefault
          e.preventDefault();
          emitShortcut(SHORTCUT_EVENTS.focusSearch);
          break;
        case "j":
          emitShortcut(SHORTCUT_EVENTS.move, { dir: 1 });
          break;
        case "k":
          emitShortcut(SHORTCUT_EVENTS.move, { dir: -1 });
          break;
        case " ":
          // 空格默认会滚动页面：只在有任务列表时接管，其它页面保持原样
          if (hasTaskList()) {
            e.preventDefault();
            emitShortcut(SHORTCUT_EVENTS.toggleSelect);
          }
          break;
        case "Enter":
          if (hasTaskList()) emitShortcut(SHORTCUT_EVENTS.openItem);
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [helpOpen, toggleHelp, closeHelp]);

  if (!mounted || !helpOpen) return null;

  return createPortal(
    <div className="fixed inset-0 z-[9999] flex items-center justify-center px-4"
      style={{ background: "rgba(4,4,8,0.6)", backdropFilter: "blur(3px)" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) closeHelp(); }}
      role="dialog" aria-modal="true" aria-label="键盘快捷键帮助">
      <div
        ref={panelRef}
        onKeyDown={(e) => trapTabKey(e, panelRef.current)}
        className="w-full max-w-md rounded-2xl border p-5 animate-fade-up"
        style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-sm">⌨️ 键盘快捷键</h2>
          <button
            ref={closeBtnRef}
            onClick={closeHelp}
            aria-label="关闭快捷键帮助"
            className="btn-ghost !px-2 !py-1 text-xs"
          >✕</button>
        </div>
        <div className="space-y-1">
          {SHORTCUTS.map((s) => (
            <div key={s.key} className="flex items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-white/[0.03]">
              <kbd className="shrink-0 min-w-12 text-center text-[11px] px-2 py-1 rounded-md"
                style={{ border: "1px solid var(--border)", background: "rgba(255,255,255,0.04)" }}>
                {s.key}
              </kbd>
              <div className="min-w-0">
                <div className="text-xs font-medium">{s.label}</div>
                <div className="text-[11px]" style={{ color: "var(--muted)" }}>{s.desc}</div>
              </div>
            </div>
          ))}
        </div>
        <p className="text-[11px] mt-4" style={{ color: "var(--muted)" }}>
          在输入框里打字时快捷键自动让位（? 除外）；命令面板打开时所有按键归面板。
        </p>
      </div>
    </div>,
    document.body,
  );
}
