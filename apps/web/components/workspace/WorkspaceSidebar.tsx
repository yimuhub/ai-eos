"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { NAV_GROUPS } from "@/lib/nav";
import { openCommandPalette } from "@/components/CommandPalette";
import { sessionMeta, type WsAgent, type WsSession } from "@/lib/workspace";

/** 左栏：数字员工 + 作战会话 + 常用页面。宽度由外层控制，这里只管内容。 */

const QUICK_PAGES = ["指挥中心", "任务", "项目中心", "知识库", "交付中心", "成果中心"];

function quickItems() {
  const all = NAV_GROUPS.flatMap((g) => g.items);
  return QUICK_PAGES.map((label) => all.find((it) => it.label === label)).filter(Boolean) as {
    href: string; label: string; icon: string;
  }[];
}

export default function WorkspaceSidebar({
  agents,
  sessions,
  activeAgentId,
  onAgent,
  onSession,
  onNewChat,
  onCollapse,
  onLogout,
  onOpenApp,
  userName,
}: {
  agents: WsAgent[];
  sessions: WsSession[];
  activeAgentId: string;
  onAgent: (agent: WsAgent) => void;
  onSession: (session: WsSession) => void;
  onNewChat: () => void;
  onCollapse: () => void;
  onLogout: () => void;
  /** 在右栏打开功能页面，而不是整页跳走 */
  onOpenApp: (href: string) => void;
  userName: string;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const pages = quickItems();

  const closeMenu = useCallback((e: MouseEvent) => {
    if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [menuOpen, closeMenu]);

  const running = sessions.filter((s) => ["analyzing", "announcing", "executing", "delivering"].includes(s.status)).length;

  return (
    <div className="flex h-full flex-col overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="px-3 pt-3">
        <button
          type="button"
          onClick={onNewChat}
          className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs font-medium transition-colors app-no-drag"
          style={{ border: "1px solid rgba(99,102,241,0.4)", background: "rgba(99,102,241,0.12)", color: "#dcddff" }}
        >
          <span aria-hidden>＋</span>
          新对话
          <span className="ml-auto text-[9px] px-1.5 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.08)" }}>⌘N</span>
        </button>
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          className="mt-2 w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs transition-colors hover:bg-white/[0.05] app-no-drag"
          style={{ border: "1px solid var(--border)", color: "var(--muted)" }}
        >
          <span aria-hidden>⌕</span>
          <span className="flex-1 text-left">搜索页面 / 会话</span>
          <span className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</span>
        </button>
      </div>

      {menuOpen && (
        <div className="px-3 pt-2" ref={menuRef}>
          <div className="rounded-lg p-1.5 space-y-0.5" style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
            {pages.map((p) => (
              <button key={p.href} type="button" onClick={() => onOpenApp(p.href)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs hover:bg-white/[0.06]"
                style={{ color: "var(--muted)" }}>
                <span aria-hidden>{p.icon}</span>{p.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex-1 overflow-y-auto ws-scroll px-3 pb-3">
        {/* 数字员工 */}
        <div className="flex items-center justify-between px-1 pt-3.5 pb-1.5">
          <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
            数字员工 · {agents.length}
          </span>
          <button type="button" onClick={() => onOpenApp("/agents")}
            className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>管理</button>
        </div>
        <div className="space-y-0.5">
          {agents.map((a) => {
            const active = a.id === activeAgentId;
            // 后端返回的是 AgentStatus 枚举（IDLE/PAUSED/...），不是小写
            const st = String(a.status || "").toUpperCase();
            const paused = st === "PAUSED" || st === "DISABLED";
            const stateText = st === "PAUSED" ? "暂停" : st === "DISABLED" ? "停用" : st === "ERROR" ? "异常" : "在岗";
            const stateColor = st === "ERROR" ? "#f87171" : paused ? "#6b6c7a" : "#34d399";
            return (
              <button
                key={a.id}
                type="button"
                onClick={() => onAgent(a)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs text-left transition-colors ${
                  active ? "text-white" : "hover:bg-white/[0.04]"
                }`}
                style={active
                  ? { background: "rgba(99,102,241,0.14)" }
                  : { color: "var(--muted)" }}
              >
                <span className="w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-semibold shrink-0"
                  style={{ background: paused ? "rgba(255,255,255,0.08)" : "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                  {a.name.slice(0, 1)}
                </span>
                <span className="flex-1 truncate">{a.name}</span>
                <span className="text-[9px] shrink-0" style={{ color: stateColor }}>
                  {stateText}
                </span>
              </button>
            );
          })}
          {!agents.length && (
            <p className="px-2 py-1.5 text-[11px]" style={{ color: "var(--muted)" }}>还没有员工 · <button type="button" onClick={() => onOpenApp("/agents")} className="text-indigo-300">去招人</button></p>
          )}
        </div>

        {/* 作战会话 */}
        <div className="flex items-center justify-between px-1 pt-4 pb-1.5">
          <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
            作战 · {sessions.length}{running ? `（${running} 在跑）` : ""}
          </span>
          <button type="button" onClick={() => onOpenApp("/warroom")}
            className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>全部</button>
        </div>
        <div className="space-y-0.5">
          {sessions.slice(0, 12).map((s) => {
            const meta = sessionMeta(s.status);
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => onSession(s)}
                title="进入作战室看完整过程"
                className="w-full flex items-start gap-2 px-2 py-1.5 rounded-lg text-left transition-colors hover:bg-white/[0.04]"
                style={{ color: "var(--muted)" }}
              >
                <span className="mt-1 w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ background: meta.tone === "run" ? "var(--accent)" : meta.tone === "bad" ? "var(--danger)" : meta.tone === "ok" ? "var(--success)" : "#4a4b58" }} />
                <span className="flex-1 min-w-0">
                  <span className="block truncate text-xs">{s.goal}</span>
                  <span className="block text-[9px] mt-0.5" style={{ color: "#55555f" }}>
                    {meta.label} · {s.leader_name}
                  </span>
                </span>
              </button>
            );
          })}
          {!sessions.length && (
            <p className="px-2 py-1.5 text-[11px]" style={{ color: "var(--muted)" }}>
              还没有作战 · <button type="button" onClick={() => onOpenApp("/warroom")} className="text-indigo-300">下达一个目标</button>
            </p>
          )}
        </div>

        {/* 页面 */}
        <div className="px-1 pt-4 pb-1.5 text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
          页面
        </div>
        <div className="space-y-0.5">
          {pages.map((p) => (
            <button key={p.href} type="button" onClick={() => onOpenApp(p.href)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs hover:bg-white/[0.04]"
              style={{ color: "var(--muted)" }}>
              <span className="w-4 text-center" aria-hidden>{p.icon}</span>
              <span className="flex-1 truncate">{p.label}</span>
            </button>
          ))}
          <button type="button" onClick={openCommandPalette}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs hover:bg-white/[0.04]"
            style={{ color: "var(--muted)" }}>
            <span className="w-4 text-center" aria-hidden>⌘</span>
            <span className="flex-1 text-left">全部页面与操作</span>
            <span className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</span>
          </button>
        </div>
      </div>

      <div className="px-3 py-2.5 border-t" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-center gap-2">
          <span className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold"
            style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
            {userName.slice(0, 1)}
          </span>
          <div className="flex-1 min-w-0">
            <p className="text-[11px] truncate">{userName}</p>
            <p className="text-[9px]" style={{ color: "#55555f" }}>老板 · 一人公司</p>
          </div>
          <button type="button" onClick={onCollapse} title="收起左栏 ⌘B"
            className="w-7 h-7 rounded-lg flex items-center justify-center text-xs hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }} aria-label="收起左栏">‹</button>
          <button type="button" onClick={onLogout} title="退出登录"
            className="w-7 h-7 rounded-lg flex items-center justify-center text-xs hover:bg-white/[0.06] hover:text-red-300"
            style={{ color: "var(--muted)" }} aria-label="退出登录">⏻</button>
        </div>
      </div>
    </div>
  );
}
