"use client";

// 首跑任务清单：把「新用户到第一次见到产出」的 4 步钉在右下角。
//
// 设计约束：
//   - 不遮罩、不抢焦点、不阻断任何操作——右下角一张可关闭的卡片；
//   - 完成检测是真数据：建司/招人/绑模型查接口，首条消息靠 WorkspaceChat
//     广播的 aieos:first-message 事件 + localStorage 存档；
//   - 四项全绿自动收起并写入完成标记，老用户永远不看；
//   - SSR 安全：首帧一律不渲染，挂载后再读 localStorage。

import { useCallback, useEffect, useRef, useState } from "react";
import { api, getActiveCompanyId } from "@/lib/api";
import { openApp } from "@/lib/apps";
import { agentModelReady } from "@/lib/workspace";

const TOUR_KEY = "aieos_tour_done";
const FIRST_MSG_KEY = "aieos_first_msg_done";
type WsAgentLite = { model_configs?: { role?: string | null }[] | null };

type TaskItem = {
  key: string;
  label: string;
  desc: string;
  href: string;
};

/** 首条消息事件由工作台对话发出（发送成功即广播一次）。 */
export function markFirstMessageDone() {
  try { localStorage.setItem(FIRST_MSG_KEY, "1"); } catch { /* 隐私模式 */ }
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("aieos:first-message"));
  }
}

export default function FirstRunTour() {
  const [visible, setVisible] = useState(false);
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const panelRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const runChecks = useCallback(async () => {
    const next: Record<string, boolean> = {};
    try {
      next.first_msg = localStorage.getItem(FIRST_MSG_KEY) === "1";
    } catch { /* 忽略 */ }
    try {
      const companies = await api.get<{ id: string }[]>("/companies", { silent: true });
      const cid = getActiveCompanyId() || companies[0]?.id || "";
      next.company = companies.length > 0;
      if (cid) {
        const agents = await api.get<WsAgentLite[]>(`/agents?company_id=${cid}`, { silent: true });
        next.agent = (agents || []).length > 0;
        next.model = (agents || []).some((a) => {
          const list = a.model_configs;
          return Array.isArray(list) && list.some((m) => (m?.role || "primary") === "primary");
        });
      } else {
        next.agent = false;
        next.model = false;
      }
    } catch {
      // 接口失败不翻状态，下一轮再查
      return;
    }
    setChecks((prev) => {
      const merged = { ...prev, ...next };
      if (Object.keys(merged).length >= 4 &&
          Object.values(merged).every(Boolean)) {
        try { localStorage.setItem(TOUR_KEY, "1"); } catch { /* 忽略 */ }
        setVisible(false);
      }
      return merged;
    });
  }, []);

  useEffect(() => {
    try {
      if (localStorage.getItem(TOUR_KEY)) return;
      const active = document.activeElement;
      returnFocusRef.current = active instanceof HTMLElement ? active : null;
      setVisible(true);
    } catch {
      setVisible(true);
    }
    void runChecks();
  }, [runChecks]);

  // 30s 轮询 + 首条消息事件 + 窗口聚焦（用户可能刚在别的页面完成步骤）
  useEffect(() => {
    if (!visible) return;
    const t = setInterval(() => void runChecks(), 30000);
    const onMsg = () => void runChecks();
    const onFocus = () => void runChecks();
    window.addEventListener("aieos:first-message", onMsg);
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(t);
      window.removeEventListener("aieos:first-message", onMsg);
      window.removeEventListener("focus", onFocus);
    };
  }, [visible, runChecks]);

  const finish = useCallback(() => {
    setVisible(false);
    try { localStorage.setItem(TOUR_KEY, "1"); } catch { /* 忽略 */ }
    const panel = panelRef.current;
    const active = document.activeElement;
    if (panel && active instanceof HTMLElement && panel.contains(active)) {
      const target = returnFocusRef.current;
      if (target?.isConnected && target !== document.body) target.focus();
      else active.blur();
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); finish(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, finish]);

  if (!visible) return null;

  const TASKS: TaskItem[] = [
    { key: "company", label: "建一家公司", desc: "员工、任务、指标都挂在公司下面", href: "/companies" },
    { key: "agent", label: "招第一批数字员工", desc: "手工建或用 Agent 工厂批量生成", href: "/agents" },
    { key: "model", label: "接模型并绑给员工", desc: "没绑模型的员工接活会失败", href: "/models" },
    { key: "first_msg", label: "发出第一条指令", desc: "在工作台说一句话，看产出落在右栏", href: "/" },
  ];
  const doneCount = TASKS.filter((t) => checks[t.key]).length;
  const allDone = doneCount === TASKS.length;

  return (
    <div
      ref={panelRef}
      role="region"
      aria-label="新手任务清单"
      className="fixed bottom-4 right-4 z-[9000] w-[330px] max-w-[calc(100vw-2rem)] rounded-2xl border shadow-2xl p-4 animate-fade-up"
      style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold tracking-widest uppercase" style={{ color: "#8b8bff" }}>
          开机清单 {doneCount}/{TASKS.length}
        </span>
        <button
          type="button"
          onClick={finish}
          aria-label="关闭任务清单"
          className="text-[10px] px-1.5 py-0.5 rounded transition-colors hover:bg-white/[0.06]"
          style={{ color: "var(--muted)" }}
        >
          关闭
        </button>
      </div>

      <div aria-live="polite" aria-atomic="true">
        <div className="mt-2 space-y-1.5">
          {TASKS.map((t) => {
            const done = !!checks[t.key];
            return (
              <div key={t.key}
                   className="flex items-start gap-2 rounded-lg px-2 py-1.5"
                   style={{ background: done ? "rgba(52,211,153,0.08)" : "rgba(255,255,255,0.03)" }}>
                <span
                  className="mt-0.5 w-4 h-4 rounded-full flex items-center justify-center text-[10px] shrink-0"
                  style={{
                    background: done ? "rgba(52,211,153,0.25)" : "rgba(255,255,255,0.08)",
                    color: done ? "#6ee7b7" : "var(--muted)",
                    border: done ? "1px solid rgba(52,211,153,0.5)" : "1px solid var(--border)",
                  }}
                  aria-hidden>
                  {done ? "✓" : ""}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[12px] font-medium"
                       style={{ color: done ? "#6ee7b7" : "var(--foreground)",
                                textDecoration: done ? "line-through" : "none" }}>
                    {t.label}
                  </div>
                  <div className="text-[10.5px] mt-0.5" style={{ color: "var(--muted)" }}>{t.desc}</div>
                </div>
                {!done && (
                  <button type="button" onClick={() => openApp(t.href)}
                    className="shrink-0 text-[10.5px] px-2 py-1 rounded-lg hover:bg-white/[0.08]"
                    style={{ color: "#a5a6ff", border: "1px solid var(--border)" }}>
                    去完成
                  </button>
                )}
              </div>
            );
          })}
        </div>
        {allDone && (
          <p className="mt-2 text-[11px]" style={{ color: "#6ee7b7" }}>
            🎉 全部就绪——从今天起，它们就是你的团队。
          </p>
        )}
      </div>
    </div>
  );
}
