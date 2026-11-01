"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SIDEBAR_GROUPS } from "@/lib/nav";
import { navFor } from "@/lib/apps";
import { openAllPages, openCommandPalette } from "@/lib/palette";
import { openModeChooser } from "@/components/ModeChooser";
import { getActiveCompanyId } from "@/lib/api";
import {
  emptySessionPrefs, flushSessionPrefs, loadSessionPrefs, markRead, mergeReadAt,
  saveSessionPrefs, toggleArchive, togglePin, unreadKeys, type SessionPrefs,
} from "@/lib/sessionPrefs";
import {
  flushThreadReads, queueThreadRead, readsToMap, startThreadReadPolling,
  type ThreadRead,
} from "@/lib/threadReads";
import {
  groupThreadsByProject, meetingMeta, sessionMeta,
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
  /** C2 按项目分组：作战会话取 project_id，项目群取项目 id，其余为空 */
  projectId?: string | null;
};

/** 「按项目分组」开关的本地偏好（跨刷新保留） */
const GROUP_BY_PROJECT_KEY = "aieos_ws_group_by_project";

const TONE_DOT: Record<string, string> = {
  run: "var(--accent)", warn: "#fbbf24", ok: "var(--success)", bad: "var(--danger)", idle: "#4a4b58",
};

/**
 * 会话偏好的 localStorage 隔离键：公司 + 用户名。
 * 会话 key 本身已带 mode:id；再叠用户名，防止同一公司下多账号复用 key 时串档。
 * 只在 effect 里调用（getActiveCompanyId 会读 localStorage）。
 */
function sessionScope(userName: string): string {
  return `${getActiveCompanyId() || "default"}::${userName || "anon"}`;
}

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
  // 第二层轻量视图：在类型筛选之上快速收敛到置顶 / 未读
  const [focus, setFocus] = useState<"all" | "pinned" | "unread">("all");
  // 会话多了要能找得到：本地搜索 + 分批渲染（一次 50 条，避免几百个 DOM 节点）
  const [query, setQuery] = useState("");
  // 按项目分组：默认关；偏好从 localStorage 恢复（SSR 首帧保持关闭，避免水合不一致）
  const [groupByProject, setGroupByProject] = useState(false);
  const [limit, setLimit] = useState(50);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const moreBtnRef = useRef<HTMLButtonElement>(null);

  // 置顶 / 归档 / 已读：首帧与 SSR 一致（空偏好），真实数据在 effect 里读
  const [prefs, setPrefs] = useState<SessionPrefs>(emptySessionPrefs);
  const prefsRef = useRef<SessionPrefs>(prefs);
  const [prefsReady, setPrefsReady] = useState(false);
  const [scopeKey, setScopeKey] = useState("");
  const [threadMenu, setThreadMenu] = useState<string | null>(null);
  const [archivedOpen, setArchivedOpen] = useState(false);

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

  // localStorage 只能在 effect 里读：服务端与首帧都是「无偏好」，避免水合不一致
  useEffect(() => {
    try {
      setGroupByProject(window.localStorage.getItem(GROUP_BY_PROJECT_KEY) === "1");
    } catch {
      /* 隐私模式 / 禁用存储：保持默认关闭 */
    }
  }, []);

  const toggleGroupByProject = useCallback(() => {
    setGroupByProject((prev) => {
      const next = !prev;
      try { window.localStorage.setItem(GROUP_BY_PROJECT_KEY, next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }, []);

  useEffect(() => {
    const sync = () => setScopeKey(sessionScope(userName));
    sync();
    // 切公司时换成新 scope，不与上一家公司串档
    window.addEventListener("aieos-company-changed", sync);
    return () => window.removeEventListener("aieos-company-changed", sync);
  }, [userName]);

  useEffect(() => {
    if (!scopeKey) return;
    const loaded = loadSessionPrefs(scopeKey);
    prefsRef.current = loaded;
    setPrefs(loaded);
    setPrefsReady(true);
  }, [scopeKey]);

  // 卸载前把防抖队列落盘，避免最后一次置顶/已读丢失
  useEffect(() => () => { flushSessionPrefs(); }, []);

  // 行内「…」菜单：点空白处 / Esc 关闭
  useEffect(() => {
    if (!threadMenu) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element | null;
      if (t?.closest?.("[data-thread-menu]")) return;
      setThreadMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setThreadMenu(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [threadMenu]);

  const threads = useMemo(() => {
    const out: Thread[] = [];
    for (const s of sessions) {
      const meta = sessionMeta(s.status);
      out.push({
        key: `war:${s.id}`, mode: "war", id: s.id,
        title: s.goal, sub: `${meta.label} · ${s.leader_name}`, tone: meta.tone, glyph: "战",
        projectId: s.project_id ?? null,
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
        projectId: p.id,
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

  const pinnedSet = useMemo(() => new Set(prefs.pinned), [prefs.pinned]);
  const archivedSet = useMemo(() => new Set(prefs.archived), [prefs.archived]);
  const unread = useMemo(() => unreadKeys(prefs, threads), [prefs, threads]);

  // 置顶优先；其余保持 threads 原有排序。归档默认不进主列表。
  const ordered = useMemo(() => {
    const alive = threads.filter((t) => !archivedSet.has(t.key));
    const pinned = alive.filter((t) => pinnedSet.has(t.key));
    if (!pinned.length) return alive;
    return [...pinned, ...alive.filter((t) => !pinnedSet.has(t.key))];
  }, [threads, pinnedSet, archivedSet]);

  const archivedThreads = useMemo(
    () => threads.filter((t) => archivedSet.has(t.key)),
    [threads, archivedSet],
  );

  // 「全部 / 置顶 / 未读」快速收敛，再叠加类型筛选与搜索
  const focusPool = useMemo(() => {
    if (focus === "pinned") return ordered.filter((t) => pinnedSet.has(t.key));
    if (focus === "unread") return ordered.filter((t) => unread.has(t.key));
    return ordered;
  }, [ordered, focus, pinnedSet, unread]);

  const visible = filter === "all" ? focusPool : focusPool.filter((t) => t.mode === filter);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return visible;
    // 标题（员工名/会议名/项目名）+ 副标题（负责人、状态）一起匹配
    return visible.filter((t) => `${t.title} ${t.sub}`.toLowerCase().includes(q));
  }, [visible, query]);

  const archivedFiltered = useMemo(() => {
    const byMode = filter === "all" ? archivedThreads : archivedThreads.filter((t) => t.mode === filter);
    const q = query.trim().toLowerCase();
    if (!q) return byMode;
    return byMode.filter((t) => `${t.title} ${t.sub}`.toLowerCase().includes(q));
  }, [archivedThreads, filter, query]);

  // 换过滤条件 / 搜索词 / 视图时回到第一批，避免"显示更多"把列表卡在几百条
  useEffect(() => { setLimit(50); }, [query, filter, focus]);
  const shown = filtered.slice(0, limit);
  // 分组作用于「已过滤 + 已排序 + 已分页」的结果，置顶/未读/搜索/类型筛选行为不变
  const grouped = useMemo(
    () => (groupByProject ? groupThreadsByProject(shown, projects) : []),
    [groupByProject, shown, projects],
  );
  const running = ordered.filter((t) => t.tone === "run").length;
  const pinnedCount = ordered.filter((t) => pinnedSet.has(t.key)).length;
  const unreadCount = ordered.filter((t) => unread.has(t.key)).length;
  const projectTop = projects.slice(0, 4);

  const isActive = (t: Thread) => view.mode === t.mode && view.id === t.id;

  // 用 ref 拿最新偏好再更新：连续两次点击菜单不会互相覆盖，也不在 setState 里做副作用
  const applyPrefs = useCallback((next: SessionPrefs) => {
    if (next === prefsRef.current) return;
    prefsRef.current = next;
    setPrefs(next);
    if (scopeKey) saveSessionPrefs(scopeKey, next);
  }, [scopeKey]);

  const markThreadRead = useCallback((key: string) => {
    if (!prefsReady || !key) return;
    const at = Date.now();
    applyPrefs(markRead(prefsRef.current, key, at));
    const cid = getActiveCompanyId();
    if (cid) queueThreadRead(cid, key, at);
  }, [prefsReady, applyPrefs]);

  // C3：已读位置跨设备同步。挂载时把本地记录上传（只传最近 30 天），
  // 之后每 60s / 回到前台拉取远端并取较大时间戳合并，绝不把已读退回未读。
  useEffect(() => {
    if (!prefsReady || !scopeKey) return;
    const cid = getActiveCompanyId();
    if (!cid) return;

    const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
    for (const [key, at] of Object.entries(prefsRef.current.readAt)) {
      if (at >= cutoff) queueThreadRead(cid, key, at);
    }

    const mergeRemote = (rows: ThreadRead[]) => {
      const merged = mergeReadAt(prefsRef.current, readsToMap(rows));
      if (merged !== prefsRef.current) applyPrefs(merged);
    };
    const stop = startThreadReadPolling(mergeRemote, cid);
    return () => {
      stop();
      void flushThreadReads();
    };
  }, [prefsReady, scopeKey, applyPrefs]);

  // 当前激活会话自动标已读（view 变化时）
  useEffect(() => {
    if (!prefsReady || !view.id) return;
    markThreadRead(`${view.mode}:${view.id}`);
  }, [view.mode, view.id, prefsReady, markThreadRead]);

  const openThread = (t: Thread) => {
    setThreadMenu(null);
    markThreadRead(t.key);
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

  // 一行 = 会话按钮 + 「…」菜单。主列表与「已归档」共用，避免两套样式走样。
  const renderThread = (t: Thread, archived = false) => {
    const active = isActive(t);
    const isUnread = unread.has(t.key);
    const pinned = pinnedSet.has(t.key);
    const menuOpen = threadMenu === t.key;
    return (
      <div key={t.key} className="group/row relative" data-thread-menu={t.key}>
        <button
          type="button"
          data-thread={t.mode}
          data-unread={isUnread ? "1" : undefined}
          onClick={() => openThread(t)}
          title={isUnread ? `${t.title}（未读）` : t.title}
          className={`w-full flex items-start gap-2 pl-2 pr-7 py-1.5 rounded-lg text-left transition-colors ${
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
              {isUnread && (
                <span className="w-1.5 h-1.5 rounded-full shrink-0" aria-label="未读"
                  style={{ background: "var(--accent)" }} />
              )}
              <span className={`flex-1 truncate text-xs ${isUnread ? "font-semibold" : ""}`}
                style={isUnread && !active ? { color: "#e3e4ee" } : undefined}>
                {t.title}
              </span>
              {pinned && (
                <span className="text-[9px] leading-none shrink-0" title="已置顶" aria-label="已置顶"
                  style={{ color: "#818cf8" }}>↑</span>
              )}
              <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: TONE_DOT[t.tone] }} />
            </span>
            <span className="block text-[9px] mt-0.5 truncate" style={{ color: "#55555f" }}>{t.sub}</span>
          </span>
        </button>

        <button type="button" onClick={() => setThreadMenu(menuOpen ? null : t.key)}
          aria-label={`${t.title} 更多操作`} aria-expanded={menuOpen} title="置顶 / 归档"
          className={`absolute right-1 top-1.5 w-5 h-5 rounded flex items-center justify-center text-[11px] hover:bg-white/15 ${
            menuOpen ? "opacity-100 bg-white/10" : "opacity-0 group-hover/row:opacity-100 focus:opacity-100 max-[900px]:opacity-70"
          }`}
          style={{ color: "#6b6c7a" }}>
          ⋯
        </button>

        {menuOpen && (
          <div className="absolute right-1 top-7 z-30 w-28 rounded-lg p-1 shadow-2xl"
            style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
            <button type="button" data-action="pin"
              onClick={() => { setThreadMenu(null); applyPrefs(togglePin(prefsRef.current, t.key)); }}
              className="w-full text-left px-2 py-1.5 rounded text-[11px] hover:bg-white/[0.08]"
              style={{ color: "var(--muted)" }}>
              {pinned ? "取消置顶" : "置顶"}
            </button>
            <button type="button" data-action={archived ? "unarchive" : "archive"}
              onClick={() => { setThreadMenu(null); applyPrefs(toggleArchive(prefsRef.current, t.key)); }}
              className="w-full text-left px-2 py-1.5 rounded text-[11px] hover:bg-white/[0.08]"
              style={{ color: "var(--muted)" }}>
              {archived ? "取消归档" : "归档"}
            </button>
          </div>
        )}
      </div>
    );
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

        <div className="relative mt-2">
          <span aria-hidden className="absolute left-2 top-1/2 -translate-y-1/2 text-[10px]"
            style={{ color: "#55555f" }}>⌕</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索会话 / 员工…"
            aria-label="搜索会话或员工"
            className="w-full pl-6 pr-6 py-1.5 rounded-lg text-[11px] bg-transparent outline-none"
            style={{ border: "1px solid var(--border)", color: "var(--foreground)" }}
          />
          {query && (
            <button type="button" onClick={() => setQuery("")} aria-label="清空搜索"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 w-4 h-4 rounded text-[11px] flex items-center justify-center hover:bg-white/10"
              style={{ color: "var(--muted)" }}>×</button>
          )}
        </div>

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
          <button type="button" data-group-by-project={groupByProject ? "1" : "0"}
            aria-pressed={groupByProject} onClick={toggleGroupByProject}
            title="按项目分组显示会话（偏好会记住）"
            className="ml-auto shrink-0 text-[10px] px-1.5 py-1 rounded-md transition-colors whitespace-nowrap"
            style={groupByProject
              ? { background: "rgba(16,185,129,0.16)", color: "#a7f3d0", border: "1px solid rgba(16,185,129,0.4)" }
              : { color: "var(--muted)", border: "1px solid var(--border)" }}>
            ▤ 项目分组
          </button>
        </div>

        {/* 第二层：在类型筛选之上按「置顶 / 未读」快速收敛，50+ 会话也能一眼定位 */}
        <div className="flex items-center gap-1 mt-1.5 overflow-x-auto ws-scroll pb-0.5">
          {([
            { key: "all", label: "全部会话" },
            { key: "pinned", label: pinnedCount ? `置顶 ${pinnedCount}` : "置顶" },
            { key: "unread", label: unreadCount ? `未读 ${unreadCount}` : "未读" },
          ] as const).map((f) => {
            const on = focus === f.key;
            return (
              <button key={f.key} type="button" data-focus={f.key} onClick={() => setFocus(f.key)}
                className="text-[9px] px-2 py-0.5 rounded-full shrink-0 transition-colors"
                style={on
                  ? { background: "rgba(34,211,238,0.16)", color: "#bdeef7", border: "1px solid rgba(34,211,238,0.4)" }
                  : { color: "#6b6c7a", border: "1px solid var(--border)" }}>
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
            进行中 · {query.trim() ? filtered.length : visible.length}{running ? `（${running} 在跑）` : ""}
          </span>
          <button type="button" onClick={() => onOpenApp("/warroom")}
            className="text-[9px] hover:text-gray-300" style={{ color: "#55555f" }}>全部</button>
        </div>
        <div className="space-y-0.5">
          {groupByProject
            ? grouped.map((g) => (
              <div key={g.projectId ?? "__ungrouped"} data-project-group={g.name}>
                <div className="flex items-center gap-1.5 px-1 pt-2 pb-1">
                  <span className="text-[9px] font-semibold tracking-[0.12em] truncate"
                    style={{ color: "#6b6c7a" }}>{g.name}</span>
                  <span className="text-[9px] tabular-nums shrink-0" style={{ color: "#55555f" }}>
                    {g.threads.length}
                  </span>
                </div>
                <div className="space-y-0.5">{g.threads.map((t) => renderThread(t))}</div>
              </div>
            ))
            : shown.map((t) => renderThread(t))}
          {!filtered.length && !(filter === "group" && chatGroups.length) && (
            <p className="px-2 py-2 text-[11px]" style={{ color: "var(--muted)" }}>
              {query.trim()
                ? `没有匹配「${query.trim()}」的会话`
                : focus === "pinned"
                  ? "还没有置顶会话，点会话右侧「…」置顶。"
                  : focus === "unread"
                    ? "没有未读会话。"
                    : archivedThreads.length && !ordered.length
                      ? "会话都已归档，展开下方「已归档」查看。"
                      : filter === "all" ? "还没有会话，先给员工下一句话。" : `没有${FILTERS.find((f) => f.key === filter)?.label}会话`}
            </p>
          )}
          {filtered.length > limit && (
            <button type="button" onClick={() => setLimit((n) => n + 50)}
              className="w-full mt-1 px-2 py-1.5 rounded-lg text-[10px] hover:bg-white/[0.04]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              显示更多（还有 {filtered.length - limit} 条）
            </button>
          )}
        </div>

        {archivedThreads.length > 0 && (
          <div className="mt-2">
            <button type="button" data-archived-toggle aria-expanded={archivedOpen}
              onClick={() => setArchivedOpen((v) => !v)}
              className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-[10px] hover:bg-white/[0.04]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              <span aria-hidden>{archivedOpen ? "▾" : "▸"}</span>
              <span className="flex-1 text-left">已归档（{archivedThreads.length}）</span>
            </button>
            {archivedOpen && (
              <div className="space-y-0.5 mt-1">
                {archivedFiltered.map((t) => renderThread(t, true))}
                {!archivedFiltered.length && (
                  <p className="px-2 py-1 text-[10px]" style={{ color: "#55555f" }}>
                    当前筛选下没有已归档会话
                  </p>
                )}
              </div>
            )}
          </div>
        )}

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
        <button type="button" onClick={openAllPages}
          className="mt-1.5 w-full flex items-center gap-2 px-2 h-7 rounded-lg text-[10px] hover:bg-white/[0.05]"
          style={{ color: "var(--muted)" }}>
          <span aria-hidden>⌘</span>
          <span className="flex-1 text-left">全部页面与操作</span>
          <span className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</span>
        </button>

        {moreOpen && (
          <div className="absolute left-3 right-3 bottom-[92px] z-30 rounded-xl p-1.5 shadow-2xl max-h-[380px] overflow-y-auto ws-scroll"
            style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
            {SIDEBAR_GROUPS.map((g) => (
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
