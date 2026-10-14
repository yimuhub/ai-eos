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

import { useCallback, useEffect, useState } from "react";
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

export default function GlobalShortcuts() {
  const [helpOpen, setHelpOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  const toggleHelp = useCallback(() => setHelpOpen((v) => !v), []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 修饰键组合一律放行：⌘K 归命令面板，其它组合归浏览器
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isPaletteOpen()) return;

      // Esc：只负责关帮助面板，不抢其它弹窗的 Esc
      if (e.key === "Escape") {
        if (helpOpen) { e.preventDefault(); setHelpOpen(false); }
        return;
      }

      const editable = isEditable(e.target);

      // 「?」在输入框里也要能开帮助——这是唯一的例外，且它不产生副作用
      if (e.key === "?") {
        e.preventDefault();
        toggleHelp();
        return;
      }
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
  }, [helpOpen, toggleHelp]);

  if (!mounted || !helpOpen) return null;

  return createPortal(
    <div className="fixed inset-0 z-[9999] flex items-center justify-center px-4"
      style={{ background: "rgba(4,4,8,0.6)", backdropFilter: "blur(3px)" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) setHelpOpen(false); }}
      role="dialog" aria-modal="true" aria-label="键盘快捷键帮助">
      <div className="w-full max-w-md rounded-2xl border p-5 animate-fade-up"
        style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-sm">⌨️ 键盘快捷键</h2>
          <button onClick={() => setHelpOpen(false)} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
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
