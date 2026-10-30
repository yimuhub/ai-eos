"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NAV_GROUPS } from "@/lib/nav";
import { navFor } from "@/lib/apps";
import { openCommandPalette } from "@/components/CommandPalette";
import { openModeChooser } from "@/components/ModeChooser";
import {
  meetingMeta, sessionMeta,
  type WsAgent, type WsChatGroup, type WsMeeting, type WsMode, type WsProject, type WsSession, type WsView,
} from "@/lib/workspace";

/** 左栏：一条作战流（单聊/群聊/会议/作战）+ 项目 + 员工 + 功能磁贴。宽度由外层控制。 */

const FILTERS: { key: WsMode | "all"; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "single", label: "单聊" },
  { key: "group", label: "群聊" },
  { key: "meeting", label: "会议" },
  { key: "war", label: "作战" },
];

const TILES: { href: string; label: string }[] = [
  { href: "/tasks", label: "任务" },
  { href: "/projects", label: "项目" },
  { href: "/knowledge", label: "知识库" },
  { href: "/delivery", label: "交付" },
  { href: "/artifacts", label: "成果" },
];

type Thread = {
  key: string;
  mode: WsMode;
  id: string;
  title: string;
  sub: string;
  tone: "run" | "warn" | "ok" | "bad" | "idle";
  glyph: string;
};

const TONE_DOT: Record<string, string> = {
  run: "var(--accent)", warn: "#fbbf24", ok: "var(--success)", bad: "var(--danger)", idle: "#4a4b58",
};

export default function WorkspaceSidebar({
  agents,
  sessions,
  projects,
  meetings,
  chatGroups,
  view,
  onAgent,
  onProject,
  onMeeting,
  onSession,
  onGroup,
  onNewChat,
  onNewGroup,
  onEditGroup,
  onCollapse,
  onLogout,
  onOpenApp,
  userName,
}: {
  agents: WsAgent[];
  sessions: WsSession[];
  projects: WsProject[];
  meetings: WsMeeting[];
  chatGroups: WsChatGroup[];
  view: WsView;
  onAgent: (agent: WsAgent) => void;
  onProject: (project: WsProject) => void;
  onMeeting: (meeting: WsMeeting) => void;
  onSession: (session: WsSession) => void;
  /** 点内置分组时可能边补齐员工边建群，由页面统一处理 */
  onGroup: (group: WsChatGroup) => void;
  onNewChat: () => void;
  onNewGroup: () => void;
  onEditGroup: (group: WsChatGroup) => void;
  onCollapse: () => void;
  onLogout: () => void;
  /** 在右栏打开功能页面，而不是整页跳走 */
  onOpenApp: (href: string) => void;
  userName: string;
}) {
  const [filter, setFilter] = useState<WsMode | "all">("all");
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const moreBtnRef = useRef<HTMLButtonElement>(null);

  const closeMore = useCallback((e: MouseEvent) => {
    const t = e.target as Node;
    if (moreRef.current?.contains(t) || moreBtnRef.current?.contains(t)) return;
    setMoreOpen(false);
  }, []);
  useEffect(() => {
    if (!moreOpen) return;
    document.addEventListener("mousedown", closeMore);
    return () => document.removeEventListener("mousedown", closeMore);
  }, [moreOpen, closeMore]);

  const threads = useMemo(() => {
    const out: Thread[] = [];
    for (const s of sessions) {
      const meta = sessionMeta(s.status);
      out.push({
        key: `war:${s.id}`, mode: "war", id: s.id,
        title: s.goal, sub: `${meta.label} · ${s.leader_name}`, tone: meta.tone, glyph: "战",
      });
    }
    for (const m of meetings) {
      const meta = meetingMeta(m.status);
      out.push({
        key: `meeting:${m.id}`, mode: "meeting", id: m.id,
        title: m.title, sub: `会议 · ${meta.label}`, tone: meta.tone, glyph: "会",
      });
    }
    for (const p of projects) {
      out.push({
        key: `group:${p.id}`, mode: "group", id: p.id,
        title: p.name, sub: p.task_total ? `项目群 · ${p.progress ?? 0}%` : "项目群", tone: "run", glyph: "群",
      });
    }
    for (const a of agents) {
      const st = String(a.status || "").toUpperCase();
      const paused = st === "PAUSED" || st === "DISABLED";
      out.push({
        key: `single:${a.id}`, mode: "single", id: a.id,
        title: a.name, sub: a.role || (paused ? "暂停" : "在岗"), tone: st === "ERROR" ? "bad" : paused ? "idle" : "ok",
        glyph: a.name.slice(0, 1),
      });
    }
    // 跑着的排前面；同组里保持原来的先后（作业流 > 会议 > 项目 > 人）
    const rank = (t: Thread) => (t.tone === "run" ? 0 : t.tone === "warn" ? 1 : t.tone === "ok" ? 2 : 3);
    return out.sort((a, b) => rank(a) - rank(b));
  }, [agents, meetings, projects, sessions]);

  const visible = filter === "all" ? threads : threads.filter((t) => t.mode === filter);
  const running = threads.filter((t) => t.tone === "run").length;
  const projectTop = projects.slice(0, 4);

  const isActive = (t: Thread) => view.mode === t.mode && view.id === t.id;
  const openThread = (t: Thread) => {
    if (t.mode === "single") {
      const a = agents.find((x) => x.id === t.id);
      if (a) onAgent(a);
    } else if (t.mode === "group") {
      const p = projects.find((x) => x.id === t.id);
      if (p) onProject(p);
    } else if (t.mode === "meeting") {
      const m = meetings.find((x) => x.id === t.id);
      if (m) onMeeting(m);
    } else {
      const s = sessions.find((x) => x.id === t.id);
      if (s) onSession(s);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="px-3 pt-3 shrink-0">
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
          onClick={openCommandPalette}
          className="mt-2 w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs transition-colors hover:bg-white/[0.05] app-no-drag"
          style={{ border: "1px solid var(--border)", color: "var(--muted)" }}
        >
          <span aria-hidden>⌕</span>
          <span className="flex-1 text-left">搜索页面 / 会话</span>
          <span className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</span>
        </button>

        <div className="flex items-center gap-1 mt-2.5 overflow-x-auto ws-scroll pb-0.5">
          {FILTERS.map((f) => {
            const on = filter === f.key;
            return (
              <button key={f.key} type="button" data-filter={f.key} onClick={() => setFilter(f.key)}
                className="text-[10px] px-2 py-1 rounded-md shrink-0 transition-colors"
                style={on
                  ? { background: "rgba(99,102,241,0.22)", color: "#e6e7ff", border: "1px solid rgba(99,102,241,0.4)" }
                  : { color: "var(--muted)", border: "1px solid var(--border)" }}>
                {f.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto ws-scroll px-3 pb-2">
        {(filter === "all" || filter === "group") && (
          <>
            <div className="flex items-center justify-between px-1 pt-3 pb-1.5">
              <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
                群聊
              </span>
              <button type="button" onClick={onNewGroup}
                className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>＋ 新建</button>
            </div>
            <div className="space-y-0.5">
              {chatGroups.map((g) => {
                const active = view.mode === "group" && view.id === g.id;
                const missing = g.missing?.length || 0;
                return (
                  <div key={g.id} className="group/row relative">
                    <button type="button" data-group={g.preset_id || "custom"} onClick={() => onGroup(g)}
                      className={`w-full flex items-start gap-2 px-2 py-1.5 rounded-lg text-left transition-colors ${
                        active ? "text-white" : "hover:bg-white/[0.04]"
                      }`}
                      style={active ? { background: "rgba(99,102,241,0.14)" } : { color: "var(--muted)" }}>
                      <span className="mt-0.5 w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-semibold shrink-0"
                        style={{ background: "rgba(255,255,255,0.06)", color: "#c9cad5" }}>
                        {g.emoji || "群"}
                      </span>
                      <span className="flex-1 min-w-0">
                        <span className="flex items-center gap-1.5">
                          <span className="flex-1 truncate text-xs">{g.name}</span>
                          {g.kind === "preset" && (
                            <span className="text-[8px] px-1 py-px rounded shrink-0"
                              style={{ background: "rgba(99,102,241,0.16)", color: "#a5b4fc" }}>内置</span>
                          )}
                        </span>
                        <span className="block text-[9px] mt-0.5 truncate" style={{ color: missing ? "#fbbf24" : "#55555f" }}>
                          {missing ? `还缺 ${missing} 位员工 · 点开自动生成` : `${g.members.length} 位成员`}
                        </span>
                      </span>
                    </button>
                    <button type="button" onClick={(e) => { e.stopPropagation(); onEditGroup(g); }}
                      title="编辑群聊" aria-label={`编辑 ${g.name}`}
                      className="absolute right-1 top-1.5 w-5 h-5 rounded flex items-center justify-center text-[10px] opacity-0 group-hover/row:opacity-100 hover:bg-white/15"
                      style={{ color: "#6b6c7a" }}>
                      ✎
                    </button>
                  </div>
                );
              })}
              {!chatGroups.length && (
                <p className="px-2 py-1.5 text-[10px]" style={{ color: "#55555f" }}>
                  还没有群聊，拉几个人开个群。
                </p>
              )}
            </div>
          </>
        )}

        <div className="flex items-center justify-between px-1 pt-3 pb-1.5">
          <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
            进行中 · {visible.length}{running ? `（${running} 在跑）` : ""}
          </span>
          <button type="button" onClick={() => onOpenApp("/warroom")}
            className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>全部</button>
        </div>
        <div className="space-y-0.5">
          {visible.slice(0, 14).map((t) => {
            const active = isActive(t);
            return (
              <button
                key={t.key}
                type="button"
                data-thread={t.mode}
                onClick={() => openThread(t)}
                className={`w-full flex items-start gap-2 px-2 py-1.5 rounded-lg text-left transition-colors ${
                  active ? "text-white" : "hover:bg-white/[0.04]"
                }`}
                style={active ? { background: "rgba(99,102,241,0.14)" } : { color: "var(--muted)" }}
              >
                <span className="mt-0.5 w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-semibold shrink-0"
                  style={{
                    background: t.mode === "single" ? "linear-gradient(135deg,#6366f1,#22d3ee)" : "rgba(255,255,255,0.06)",
                    color: t.mode === "single" ? "#fff" : "#c9cad5",
                  }}>
                  {t.glyph}
                </span>
                <span className="flex-1 min-w-0">
                  <span className="flex items-center gap-1.5">
                    <span className="flex-1 truncate text-xs">{t.title}</span>
                    <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: TONE_DOT[t.tone] }} />
                  </span>
                  <span className="block text-[9px] mt-0.5 truncate" style={{ color: "#55555f" }}>{t.sub}</span>
                </span>
              </button>
            );
          })}
          {!visible.length && !(filter === "group" && chatGroups.length) && (
            <p className="px-2 py-2 text-[11px]" style={{ color: "var(--muted)" }}>
              {filter === "all" ? "还没有会话，先给员工下一句话。" : `没有${FILTERS.find((f) => f.key === filter)?.label}会话`}
            </p>
          )}
        </div>

        {projectTop.length > 0 && (
          <>
            <div className="flex items-center justify-between px-1 pt-4 pb-1.5">
              <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>项目</span>
              <button type="button" onClick={() => onOpenApp("/projects")}
                className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>全部</button>
            </div>
            <div className="space-y-1">
              {projectTop.map((p) => {
                const pct = Math.max(0, Math.min(100, p.progress ?? 0));
                return (
                  <button key={p.id} type="button" onClick={() => onProject(p)}
                    className="w-full px-2 py-1.5 rounded-lg text-left hover:bg-white/[0.04]"
                    style={{ color: "var(--muted)" }}>
                    <span className="flex items-center gap-2 text-xs">
                      <span className="flex-1 truncate">{p.name}</span>
                      <span className="text-[9px]" style={{ color: "#55555f" }}>{pct}%</span>
                    </span>
                    <span className="block mt-1 h-[3px] rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.06)" }}>
                      <span className="block h-full rounded-full"
                        style={{ width: `${pct}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }} />
                    </span>
                  </button>
                );
              })}
            </div>
          </>
        )}

        {agents.length > 0 && (
          <>
            <div className="flex items-center justify-between px-1 pt-4 pb-1.5">
              <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>员工</span>
              <button type="button" onClick={() => onOpenApp("/agents")}
                className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>管理</button>
            </div>
            <div className="flex flex-wrap items-center gap-1 px-1">
              {agents.slice(0, 6).map((a) => (
                <button key={a.id} type="button" onClick={() => onAgent(a)} title={`${a.name}${a.role ? ` · ${a.role}` : ""}`}
                  className="w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-semibold hover:ring-2 hover:ring-indigo-400/40"
                  style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                  {a.name.slice(0, 1)}
                </button>
              ))}
              {agents.length > 6 && (
                <button type="button" onClick={() => onOpenApp("/agents")}
                  className="w-6 h-6 rounded-lg flex items-center justify-center text-[9px]"
                  style={{ background: "rgba(255,255,255,0.06)", color: "var(--muted)" }}>
                  +{agents.length - 6}
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {/* 功能磁贴 */}
      <div className="px-3 pb-2 shrink-0 relative" ref={moreRef}>
        <div className="text-[9px] font-semibold tracking-[0.16em] uppercase px-1 pb-1.5" style={{ color: "#55555f" }}>
          功能
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          {TILES.map((t) => (
            <button key={t.href} type="button" data-page={t.href} onClick={() => onOpenApp(t.href)}
              className="flex flex-col items-center gap-1 py-2 rounded-xl transition-all hover:-translate-y-px"
              style={{ background: "rgba(255,255,255,0.025)", border: "1px solid transparent", color: "var(--muted)" }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "linear-gradient(180deg,rgba(99,102,241,.16),rgba(34,211,238,.05))";
                e.currentTarget.style.borderColor = "rgba(99,102,241,.35)";
                e.currentTarget.style.color = "#e3e4ee";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(255,255,255,0.025)";
                e.currentTarget.style.borderColor = "transparent";
                e.currentTarget.style.color = "var(--muted)";
              }}>
              <span className="text-[13px] leading-none" aria-hidden>{navFor(t.href)?.icon || "◻"}</span>
              <span className="text-[10px]">{t.label}</span>
            </button>
          ))}
          <button ref={moreBtnRef} type="button" onClick={() => setMoreOpen((v) => !v)}
            className="flex flex-col items-center gap-1 py-2 rounded-xl transition-all hover:-translate-y-px"
            style={{
              background: moreOpen ? "rgba(99,102,241,0.16)" : "rgba(255,255,255,0.025)",
              border: "1px solid " + (moreOpen ? "rgba(99,102,241,0.4)" : "transparent"),
              color: moreOpen ? "#dcddff" : "var(--muted)",
            }}>
            <span className="text-[13px] leading-none" aria-hidden>⋯</span>
            <span className="text-[10px]">更多</span>
          </button>
        </div>
        <button type="button" onClick={openCommandPalette}
          className="mt-1.5 w-full flex items-center gap-2 px-2 h-7 rounded-lg text-[10px] hover:bg-white/[0.05]"
          style={{ color: "var(--muted)" }}>
          <span aria-hidden>⌘</span>
          <span className="flex-1 text-left">全部页面与操作</span>
          <span className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</span>
        </button>

        {moreOpen && (
          <div className="absolute left-3 right-3 bottom-[92px] z-30 rounded-xl p-1.5 shadow-2xl max-h-[380px] overflow-y-auto ws-scroll"
            style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
            {NAV_GROUPS.map((g) => (
              <div key={g.title}>
                <p className="text-[9px] tracking-[0.12em] uppercase px-2 pt-1.5 pb-1" style={{ color: "#55555f" }}>{g.title}</p>
                {g.items.map((it) => (
                  <button key={it.href} type="button"
                    onClick={() => { setMoreOpen(false); onOpenApp(it.href); }}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[11px] hover:bg-white/[0.06]"
                    style={{ color: "var(--muted)" }}>
                    <span className="w-4 text-center" aria-hidden>{it.icon}</span>
                    <span className="flex-1 text-left truncate">{it.label}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="px-3 py-2.5 border-t shrink-0" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-center gap-2">
          <span className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold"
            style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
            {userName.slice(0, 1)}
          </span>
          <div className="flex-1 min-w-0">
            <p className="text-[11px] truncate">{userName}</p>
            <p className="text-[9px]" style={{ color: "#55555f" }}>老板 · 一人公司</p>
          </div>
          <button type="button" onClick={openModeChooser} title="切换界面模式"
            className="w-7 h-7 rounded-lg flex items-center justify-center text-xs hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }} aria-label="切换界面模式">⇄</button>
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
