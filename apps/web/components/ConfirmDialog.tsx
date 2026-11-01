"use client";

// 统一二次确认对话框（替代原生 window.confirm）
//
// ## 为什么不能继续用原生 confirm
// 改造前全站有 20 处危险操作（删项目、删员工、批量删任务、全局紧急停止……）
// 都是 `if (!confirm("...")) return;`。它能拦住误触，但有三个真实代价：
//   1. **不可信**：浏览器原生弹窗可以被用户在设置里勾选"不再显示"，之后
//      `confirm()` 直接返回 true——"二次确认"变回一次点击就删库；
//   2. **说不清**：原生弹窗只能显示一行纯文本，无法把"将同时删除 N 条记忆 /
//      M 个成果"列清楚，用户无法判断自己在放弃什么；
//   3. **不一致**：深色产品里弹出一个浅色系统框，观感上像系统故障。
//
// ## 这里做的三件事
//   1. **影响面可见**：`bullets` 逐条列出后果，`danger` 用红色语义强调不可恢复；
//   2. **不可逆操作加码**：`confirmPhrase` 要求用户逐字输入确认词
//      （如删除公司要输入公司名）。这是"手滑"与"确认"之间的真实区别；
//   3. **键盘可用**：Esc 取消、Enter 确认、Tab 在弹窗内循环，关闭后焦点归位。
//
// ## 用法（同步写法，调用点改动极小）
// ```ts
// if (!(await confirmAction({ title: "删除员工？", danger: true }))) return;
// ```
// 宿主组件 `ConfirmHost` 挂载在 `(main)/layout.tsx`，全局只需一个实例。

import { useCallback, useEffect, useRef, useState } from "react";

export type ConfirmOptions = {
  /** 主问句。应当是一个问句，而不是陈述句 */
  title: string;
  /** 补充说明：为什么会有这一步，会丢失什么 */
  description?: string;
  /** 逐条列出的影响面（"将同时删除 N 条记忆"），比一句话更让人敢确认 */
  bullets?: string[];
  /** 确认按钮文案，默认"确认"。写成动词更清楚："删除""停止" */
  confirmText?: string;
  cancelText?: string;
  /** 危险操作：红色主按钮 + 警示图标 */
  danger?: boolean;
  /**
   * 需要用户逐字输入的确认词。设置后确认按钮在输入匹配前保持禁用。
   * 用在真正不可逆的操作上（删除公司 / 清空全部任务），
   * 不要对"移出项目团队"这类可恢复操作使用——过度设防会让用户麻木。
   */
  confirmPhrase?: string;
  /** 输入框下方的提示，说明要输入什么 */
  phraseHint?: string;
};

type Pending = { opts: ConfirmOptions; resolve: (ok: boolean) => void };
type Listener = (p: Pending) => void;

const listeners = new Set<Listener>();

const queue: Pending[] = [];
let active = false; // 是否已有一个弹窗在显示

function dispatch(p: Pending) {
  if (active) {
    // 已有弹窗：排队。绝不能覆盖——否则前一个 Promise 永远不会 settle，
    // 调用方会永久 await，表现为按钮点了没反应。
    queue.push(p);
    return;
  }
  active = true;
  listeners.forEach((fn) => fn(p));
}

/** 弹出确认框。确认返回 true，取消 / Esc / 点遮罩返回 false。 */
export function confirmAction(opts: ConfirmOptions): Promise<boolean> {
  return new Promise<boolean>((resolve) => dispatch({ opts, resolve }));
}

/** 当前是否有弹窗（供快捷键层判断，避免 Esc 同时触发别的行为） */
export function isConfirmOpen(): boolean {
  return active;
}

const FOCUSABLE = 'button:not([disabled]), input, [href], select, textarea, [tabindex]:not([tabindex="-1"])';

export default function ConfirmHost() {
  const [current, setCurrent] = useState<Pending | null>(null);
  // current 的 ref 镜像：finish 的副作用（resolve/shift）必须发生在 updater **之外**——
  // setState updater 在 StrictMode(dev) 下会被调用两次，updater 里做副作用
  // （cur.resolve + queue.shift）会 double-shift，队列里下一条确认的 Promise
  // 永远无人 resolve → 调用方 await 永久挂起（"点了没反应"）。
  const currentRef = useRef<Pending | null>(null);
  const [phraseInput, setPhraseInput] = useState("");
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const setCurrentBoth = useCallback((p: Pending | null) => {
    currentRef.current = p;
    setCurrent(p);
  }, []);

  // 订阅：宿主只处理第一个请求，后续由 finish() 从队列里取
  useEffect(() => {
    const fn: Listener = (p) => setCurrentBoth(p);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, [setCurrentBoth]);

  const finish = useCallback((ok: boolean) => {
    const cur = currentRef.current;
    if (cur) {
      try {
        cur.resolve(ok);
      } catch {
        /* 调用方的后续逻辑抛错不该影响弹窗关闭 */
      }
    }
    const next = queue.shift() ?? null;
    active = !!next;
    setCurrentBoth(next);
    setPhraseInput("");
  }, [setCurrentBoth]);

  // 打开时聚焦 + 记录来源焦点，关闭时归还焦点
  useEffect(() => {
    if (!current) return;
    restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
    // 需要输入确认词时焦点给输入框，否则给主按钮——让"下一步做什么"零思考
    const t = setTimeout(() => {
      (current.opts.confirmPhrase ? inputRef.current : primaryRef.current)?.focus();
    }, 30);
    return () => {
      clearTimeout(t);
      restoreFocusRef.current?.focus?.();
    };
  }, [current]);

  // 键盘：Esc 取消 / Enter 确认 / Tab 循环
  useEffect(() => {
    if (!current) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        finish(false);
        return;
      }
      if (e.key === "Enter") {
        // 输入确认词时 Enter 交给按钮的 disabled 状态判断，不在这里抢
        const needPhrase = !!current.opts.confirmPhrase;
        const matched = !needPhrase || phraseInput.trim() === current.opts.confirmPhrase;
        if (!needPhrase && matched) {
          e.preventDefault();
          finish(true);
        }
        return;
      }
      if (e.key === "Tab") {
        const root = dialogRef.current;
        if (!root) return;
        const nodes = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
          (el) => !el.hasAttribute("disabled") && el.offsetParent !== null,
        );
        if (!nodes.length) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        const cur = document.activeElement as HTMLElement | null;
        if (e.shiftKey && (cur === first || !root.contains(cur))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && cur === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    // capture：抢在其它全局快捷键（如 ⌘K）之前处理 Esc
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [current, phraseInput, finish]);

  if (!current) return null;

  const o = current.opts;
  const needPhrase = !!o.confirmPhrase;
  const phraseMatched = !needPhrase || phraseInput.trim() === o.confirmPhrase;
  const danger = o.danger !== false; // 默认按危险处理：漏配的代价是"太谨慎"，而不是"删错了"

  const accent = danger ? "var(--danger)" : "var(--accent)";
  // 描述可能来自 description、bullets，或两者兼有：只指向真实存在的节点，
  // 否则读屏会拿到一个空引用（旧实现只写死 confirm-desc，只有 bullets 时读不到任何后果）。
  const describedBy =
    [
      o.description ? "confirm-desc" : null,
      o.bullets?.length ? "confirm-bullets" : null,
    ]
      .filter(Boolean)
      .join(" ") || undefined;

  return (
    <div
      className="fixed inset-0 z-[10000] flex items-center justify-center p-4"
      style={{ background: "rgba(4,4,8,0.66)", backdropFilter: "blur(3px)" }}
      onMouseDown={(e) => {
        // 只认「按下就落在遮罩上」：避免在弹窗内选文字、拖到遮罩上误关
        if (e.target === e.currentTarget) finish(false);
      }}
    >
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby={describedBy}
        className="w-full max-w-md rounded-2xl border p-5 shadow-2xl animate-fade-up"
        style={{
          background: "linear-gradient(180deg, #16161f, #101018)",
          borderColor: danger ? "rgba(248,113,113,0.35)" : "var(--border)",
        }}
      >
        <div className="flex items-start gap-3">
          <div
            aria-hidden
            className="w-9 h-9 shrink-0 rounded-xl flex items-center justify-center text-base"
            style={{
              background: danger ? "rgba(248,113,113,0.12)" : "rgba(99,102,241,0.12)",
              border: `1px solid ${danger ? "rgba(248,113,113,0.3)" : "rgba(99,102,241,0.3)"}`,
            }}
          >
            {danger ? "⚠️" : "❓"}
          </div>
          <div className="flex-1 min-w-0">
            <h2 id="confirm-title" className="text-[15px] font-semibold leading-6">
              {o.title}
            </h2>
            {o.description && (
              <p id="confirm-desc" className="text-xs mt-1.5 leading-5" style={{ color: "var(--muted)" }}>
                {o.description}
              </p>
            )}
          </div>
        </div>

        {!!o.bullets?.length && (
          <ul
            id="confirm-bullets"
            className="mt-3.5 space-y-1.5 rounded-lg p-3 text-xs leading-5"
            style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}
          >
            {o.bullets.map((b, i) => (
              <li key={i} className="flex gap-2">
                <span aria-hidden style={{ color: accent }}>•</span>
                <span style={{ color: "var(--ink-2, #c9c9d4)" }}>{b}</span>
              </li>
            ))}
          </ul>
        )}

        {needPhrase && (
          <div className="mt-3.5">
            <label htmlFor="confirm-phrase" className="text-xs" style={{ color: "var(--muted)" }}>
              {o.phraseHint || `请输入「${o.confirmPhrase}」以确认`}
            </label>
            <input
              id="confirm-phrase"
              ref={inputRef}
              className="input mt-1.5"
              value={phraseInput}
              onChange={(e) => setPhraseInput(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder={o.confirmPhrase}
            />
          </div>
        )}

        <div className="flex justify-end gap-2 mt-5">
          <button type="button" className="btn-ghost" onClick={() => finish(false)}>
            {o.cancelText || "取消"}
          </button>
          <button
            ref={primaryRef}
            type="button"
            disabled={!phraseMatched}
            onClick={() => finish(true)}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white transition-all duration-200"
            style={{
              background: danger
                ? "linear-gradient(135deg, #ef4444, #b91c1c)"
                : "linear-gradient(135deg, #6366f1, #8b5cf6)",
              opacity: phraseMatched ? 1 : 0.4,
              cursor: phraseMatched ? "pointer" : "not-allowed",
              boxShadow: phraseMatched ? "0 2px 12px -2px rgba(0,0,0,0.4)" : "none",
            }}
          >
            {o.confirmText || "确认"}
          </button>
        </div>
      </div>
    </div>
  );
}
