"use client";

// 首次进入经典界面时的 3 步轻量引导。
//
// 设计约束：
//   - 不遮罩、不抢焦点、不阻断任何操作——它只是右下角一张可关闭的卡片；
//   - 只在 `aieos_tour_done` 不存在时出现，完成后写入 localStorage，老用户永远不看；
//   - SSR 安全：首帧一律不渲染，挂载后再读 localStorage，避免水合不一致；
//   - 键盘可用：Esc 关闭；焦点原本在哪就留在哪，关闭时若焦点落在卡片内则归还。

import { useCallback, useEffect, useRef, useState } from "react";

const TOUR_KEY = "aieos_tour_done";

const STEPS = [
  {
    title: "左栏是全部能力入口",
    desc: "每天要用的页面都在左栏；一时找不到的页面，点「全部页面」或按 ⌘K 直接搜。",
  },
  {
    title: "中间一句话下指令",
    desc: "在「工作台」中间说清你要什么，AI 会拆任务、派给合适的员工。",
  },
  {
    title: "右栏看产出/交付",
    desc: "员工跑完的成果、交付物和执行过程都在右栏，随时验收。",
  },
] as const;

export default function FirstRunTour() {
  const [visible, setVisible] = useState(false);
  const [step, setStep] = useState(0);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const nextRef = useRef<HTMLButtonElement | null>(null);
  /** 引导出现前的焦点；关闭时若焦点还在卡片里，归还给它。 */
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    try {
      if (localStorage.getItem(TOUR_KEY)) return;
      const active = document.activeElement;
      returnFocusRef.current = active instanceof HTMLElement ? active : null;
      setVisible(true);
    } catch {
      // 隐私模式下读不到 localStorage：仍然展示一次，用户可关掉
      setVisible(true);
    }
  }, []);

  const finish = useCallback(() => {
    const panel = panelRef.current;
    const active = document.activeElement;
    setVisible(false);
    try {
      localStorage.setItem(TOUR_KEY, "1");
    } catch {
      /* 写不进去就下次再看一遍，不影响当前操作 */
    }
    if (panel && active instanceof HTMLElement && panel.contains(active)) {
      const target = returnFocusRef.current;
      if (target?.isConnected && target !== document.body) target.focus();
      else active.blur();
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, finish]);

  // 切到下一步时把焦点留在主按钮上，键盘用户可以连续按 Enter
  useEffect(() => {
    if (!visible || step === 0) return;
    nextRef.current?.focus();
  }, [visible, step]);

  if (!visible) return null;

  const isLast = step === STEPS.length - 1;
  const current = STEPS[step];

  return (
    <div
      ref={panelRef}
      role="region"
      aria-label="新手引导"
      className="fixed bottom-4 right-4 z-[9000] w-[320px] max-w-[calc(100vw-2rem)] rounded-2xl border shadow-2xl p-4 animate-fade-up"
      style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "#8b8bff" }}>
          新手引导 {step + 1}/{STEPS.length}
        </span>
        <button
          type="button"
          onClick={finish}
          aria-label="跳过新手引导"
          className="text-[10px] px-1.5 py-0.5 rounded transition-colors hover:bg-white/[0.06]"
          style={{ color: "var(--muted)" }}
        >
          跳过
        </button>
      </div>

      <div aria-live="polite" aria-atomic="true">
        <h3 className="text-sm font-semibold mt-2">{current.title}</h3>
        <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
          {current.desc}
        </p>
      </div>

      <div className="flex items-center justify-between gap-2 mt-3.5">
        <div className="flex items-center gap-1" aria-hidden>
          {STEPS.map((_, i) => (
            <span
              key={i}
              className="h-1 rounded-full transition-all"
              style={{
                width: i === step ? 18 : 6,
                background: i <= step ? "linear-gradient(90deg,#6366f1,#22d3ee)" : "var(--border)",
              }}
            />
          ))}
        </div>
        <div className="flex items-center gap-1.5">
          {step > 0 && (
            <button
              type="button"
              onClick={() => setStep((s) => Math.max(0, s - 1))}
              className="text-[11px] px-2.5 py-1.5 rounded-lg transition-colors hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}
            >
              上一步
            </button>
          )}
          <button
            ref={nextRef}
            type="button"
            onClick={() => (isLast ? finish() : setStep((s) => s + 1))}
            className="text-[11px] px-3 py-1.5 rounded-lg font-medium transition-colors"
            style={{ background: "rgba(99,102,241,0.18)", border: "1px solid rgba(99,102,241,0.45)", color: "#dcddff" }}
          >
            {isLast ? "完成" : "下一步"}
          </button>
        </div>
      </div>
    </div>
  );
}
