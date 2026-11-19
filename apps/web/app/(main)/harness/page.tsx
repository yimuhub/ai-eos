"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, clearToken, describeError, getActiveCompanyId, setActiveCompanyId } from "@/lib/api";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";
import { openCommandPalette, pushRecentPage, openAllPages } from "@/lib/palette";
import { openModeChooser } from "@/components/ModeChooser";
import { navFor, registerAppOpener, stripEmbed, type AppTab } from "@/lib/apps";
import { readUIMode } from "@/lib/ui-mode";
import Splitter from "@/components/workspace/Splitter";
import WorkspaceChat from "@/components/workspace/WorkspaceChat";
import WorkspacePanel from "@/components/workspace/WorkspacePanel";
import GroupEditor from "@/components/workspace/GroupEditor";
import FirstRunTour from "@/components/FirstRunTour";
import type { OutTab } from "@/components/workspace/WorkspaceOutput";
import { agentModelReady,
  type WsAgent, type WsChatGroup, type WsMeeting, type WsMode, type WsProject, type WsSession, type WsView } from "@/lib/workspace";

/**
 * Harness 指挥台（第三种界面形态）。
 *
 * 外壳按 ZCode 交互标准重造：46px 图标活动栏 + 上下文侧栏 + 底部状态栏 + ⌘K；
 * 中栏对话与右栏「产出/页面标签」直接复用工作台的 WorkspaceChat / WorkspacePanel——
 * 单聊/群聊/会议/作战、trace、iframe 标签与水合看门狗全部原样保留，
 * 再用一层作用域 CSS 变量把整个外壳换成石墨黑 + 暖橙的 Harness 审美。
 *
 * 与工作台的差异（这里是 Harness 自己的约定）：
 *   - 「展开」永远有反应：没有页面时展开 = 产出区吃满中间与右侧（对话挂起不卸载）；
 *   - 展开/收起通过 hidden class 切换，对话与 iframe 标签保持挂载，不重载；
 *   - 三栏宽度可拖拽（Splitter），独立存档，与工作台互不干扰。
 */

const LAYOUT_KEY = "aieos_harness_layout";
const AGENT_KEY = "aieos_workspace_agent"; // 与工作台共用"当前员工"，两个壳无缝衔接
const APPS_KEY = "aieos_harness_apps";

const LEFT_MIN = 208;
const LEFT_MAX = 380;
const RIGHT_MIN = 360;
const RIGHT_MAX = 760;
const CENTER_MIN = 420;
const SPLITTER_W = 7;
const PAGE_RIGHT_MIN = 360;
const SIDEBAR_W = 248;

const DEFAULT_LAYOUT = { left: 248, right: 460, pageRight: 0, leftHidden: false, rightHidden: false };
type LayoutState = typeof DEFAULT_LAYOUT;

function readLayout(): LayoutState {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const d = JSON.parse(raw) || {};
    return {
      left: Number.isFinite(d.left) ? d.left : DEFAULT_LAYOUT.left,
      right: Number.isFinite(d.right) ? d.right : DEFAULT_LAYOUT.right,
      pageRight: Number.isFinite(d.pageRight) ? d.pageRight : 0,
      leftHidden: !!d.leftHidden,
      rightHidden: !!d.rightHidden,
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

function appTab(href: string): AppTab {
  const nav = navFor(href);
  return { id: href, href, src: href, label: nav?.label || href, icon: nav?.icon || "◻" };
}

/** 开机恢复：先看 `?app=`（分享链接、刷新），再看上次的标签存档。 */
function bootApps(): { apps: AppTab[]; pane: string } {
  if (typeof window === "undefined") return { apps: [], pane: "out" };
  // 经典模式不恢复标签：这一页只是三栏对话，页面靠整页跳转
  if (readUIMode() === "classic") return { apps: [], pane: "out" };
  let saved: AppTab[] = [];
  let savedActive = "";
  try {
    const raw = localStorage.getItem(APPS_KEY);
    const d = raw ? JSON.parse(raw) : null;
    if (Array.isArray(d?.hrefs)) {
      const seen = new Set<string>();
      saved = d.hrefs
        .filter((h: unknown) => typeof h === "string" && h.startsWith("/"))
        .map((h: string) => stripEmbed(h))
        .filter((h: string) => (seen.has(h) ? false : (seen.add(h), true)))
        .slice(0, 8)
        .map((h: string) => appTab(h));
    }
    if (typeof d?.active === "string") savedActive = d.active;
  } catch {
    /* 存档损坏就当作没开过标签 */
  }
  const fromQuery = new URLSearchParams(window.location.search).get("app");
  if (fromQuery && fromQuery.startsWith("/")) {
    const tab = appTab(stripEmbed(fromQuery));
    const exists = saved.some((t) => t.id === tab.id);
    return { apps: exists ? saved : [...saved, tab], pane: tab.id };
  }
  const active = saved.find((t) => t.id === savedActive || t.href === savedActive);
  return { apps: saved, pane: active ? active.id : "out" };
}

/** Harness 活动栏/侧栏用的线性图标（与设计稿同一套 lucide 风格路径）。 */
const ICON_PATHS: Record<string, string> = {
  zap: "M13 2 3 14h7l-1 8 10-12h-7l1-8z",
  message: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  users:
    "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  package:
    "M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z M3.27 6.96 12 12.01l8.73-5.05 M12 22.08V12",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  check: "M22 11.08V12a10 10 0 1 1-5.93-9.14 M22 4 12 14.01l-3-3",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.35-4.35",
  "panel-left": "M3 3h18v18H3z M9 3v18",
  "panel-right": "M3 3h18v18H3z M15 3v18",
  logs: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M9 13h6 M9 17h4",
  swap: "M17 3l4 4-4 4 M21 7H9 M7 21l-4-4 4-4 M3 17h12",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9",
};

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={ICON_PATHS[name] || ICON_PATHS.zap} />
    </svg>
  );
}

/** 活动栏上的核心页面入口（其余 40+ 页面全部走 ⌘K / 侧栏「全部页面」）。 */
const RAIL_PAGES: { href: string; icon: string; label: string }[] = [
  { href: "/agents", icon: "users", label: "Agent 团队" },
  { href: "/industry-hub", icon: "grid", label: "行业中心" },
  { href: "/market", icon: "package", label: "基座市场" },
  { href: "/admin", icon: "shield", label: "超级管理后台" },
  { href: "/approvals", icon: "check", label: "审批收件箱" },
];

/** 侧栏「基座能力」区：把 v6 基座的一线能力全部摆到明面上。 */
const BASE_PAGES: { href: string; label: string }[] = [
  { href: "/industry-hub", label: "行业中心" },
  { href: "/market", label: "基座市场" },
  { href: "/connectors", label: "连接器" },
  { href: "/metrics", label: "指标中心" },
  { href: "/logs", label: "系统日志" },
  { href: "/admin", label: "超级管理后台" },
  { href: "/models", label: "模型配置" },
  { href: "/tasks", label: "任务" },
  { href: "/knowledge", label: "知识库" },
];

/**
 * Harness 外壳的作用域主题：覆盖全局 CSS 变量，让复用的对话/右栏整体换肤。
 * --ws-acc* 是工作台内脏的品牌色（头像/标签/高亮/主按钮），默认靛蓝，
 * 这里统一换成暖橙——内脏组件一行不用改。
 */
const HARNESS_THEME = {
  "--background": "#17171c",
  "--surface": "#101014",
  "--surface-2": "#1d1d24",
  "--border": "#25252d",
  "--foreground": "#e9e9ee",
  "--muted": "#9b9ba6",
  "--accent": "#f5862e",
  "--accent-2": "#ffb066",
  "--ws-acc": "245, 134, 46",
  "--ws-acc-solid": "#f5862e",
  "--ws-acc-deep": "#d84a3a",
  "--ws-acc-glow": "#ffb066",
  "--ws-acc-tint-b": "216, 74, 58",
  "--ws-acc-text": "#ffd2ad",
  "--ws-acc-text-strong": "#ffe4c9",
  "--ws-btn-grad-b": "#e05a2b",
} as React.CSSProperties;

export default function HarnessPage() {
  const router = useRouter();
  const rowRef = useRef<HTMLDivElement>(null);
  const compactRef = useRef(false);

  const [boot, setBoot] = useState<"loading" | "ready" | "failed">("loading");
  const [noCompany, setNoCompany] = useState(false);
  const [err, setErr] = useState("");
  const [cid, setCid] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [userName, setUserName] = useState("我");
  const [agents, setAgents] = useState<WsAgent[]>([]);
  const [sessions, setSessions] = useState<WsSession[]>([]);
  const [projects, setProjects] = useState<WsProject[]>([]);
  const [meetings, setMeetings] = useState<WsMeeting[]>([]);
  const [chatGroups, setChatGroups] = useState<WsChatGroup[]>([]);
  const [groupEditor, setGroupEditor] = useState<{ open: boolean; group: WsChatGroup | null }>({ open: false, group: null });
  const [groupSaving, setGroupSaving] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [view, setView] = useState<WsView>({ mode: "single", id: "" });
  const [picking, setPicking] = useState(false);
  const [compact, setCompact] = useState(false);
  // 三栏宽度与收展：数值持久化，可拖拽（与工作台同一套 Splitter）
  const [layout, setLayout] = useState<LayoutState>(DEFAULT_LAYOUT);
  const [outTab, setOutTab] = useState<OutTab>("trace");
  const [focus, setFocus] = useState(false);
  const booted = useRef<ReturnType<typeof bootApps> | null>(null);
  if (!booted.current) booted.current = bootApps();
  const [apps, setApps] = useState<AppTab[]>(booted.current.apps);
  const [pane, setPane] = useState<string>(booted.current.pane);
  const frames = useRef<Map<string, HTMLIFrameElement>>(new Map());
  const paneRef = useRef(pane);
  paneRef.current = pane;
  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;
  const autoLeft = useRef(false);
  const [rowW, setRowW] = useState(0);

  /* ---------- classic 模式误入守卫 ---------- */

  useEffect(() => {
    if (readUIMode() === "classic") router.replace("/dashboard");
  }, [router]);

  /* ---------- 数据 ---------- */

  const load = useCallback(async () => {
    setBoot("loading");
    setErr("");
    try {
      const me = await api.get<{ display_name?: string | null; username?: string | null; email?: string | null }>("/auth/me");
      setUserName(me.display_name || me.username || (me.email ? me.email.split("@")[0] : "") || "我");

      const companies = await api.get<{ id: string; name: string }[]>("/companies");
      if (!companies.length) {
        setNoCompany(true);
        setBoot("ready");
        return;
      }
      setNoCompany(false);
      const active = getActiveCompanyId();
      const company = companies.find((c) => c.id === active) || companies[0];
      setCid(company.id);
      setCompanyName(company.name);

      const [agentRows, wr, projectRows, meetingRows, groupRows] = await Promise.all([
        api.get<WsAgent[]>(`/agents?company_id=${company.id}`),
        api.get<{ sessions: WsSession[] }>(`/war-room/sessions?company_id=${company.id}`)
          .catch(() => ({ sessions: [] as WsSession[] })),
        api.get<WsProject[]>(`/projects?company_id=${company.id}`).catch(() => [] as WsProject[]),
        api.get<WsMeeting[]>(`/meetings?company_id=${company.id}`).catch(() => [] as WsMeeting[]),
        api.get<{ groups: WsChatGroup[] }>(`/chat-groups?company_id=${company.id}`)
          .catch(() => ({ groups: [] as WsChatGroup[] })),
      ]);
      setAgents(Array.isArray(agentRows) ? agentRows : []);
      setSessions(wr.sessions || []);
      setProjects(Array.isArray(projectRows) ? projectRows : []);
      setMeetings(Array.isArray(meetingRows) ? meetingRows : []);
      const groups = Array.isArray(groupRows.groups) ? groupRows.groups : [];
      setChatGroups(groups);

      let saved = "";
      try { saved = localStorage.getItem(AGENT_KEY) || ""; } catch { /* 忽略 */ }
      const prevAgent = agentIdRef.current;
      const chosen = prevAgent && agentRows.some((a) => a.id === prevAgent)
        ? prevAgent
        : saved && agentRows.some((a) => a.id === saved)
          ? saved
          : agentRows[0]?.id || "";
      setAgentId(chosen);
      agentIdRef.current = chosen;
      setView((prev) => {
        const list: { id: string }[] = prev.mode === "group" ? [...projectRows, ...groups]
          : prev.mode === "meeting" ? meetingRows
          : prev.mode === "war" ? wr.sessions || []
          : agentRows;
        if (prev.id && list.some((r) => r.id === prev.id)) return prev;
        return { mode: "single", id: chosen };
      });
      setBoot("ready");
    } catch (e) {
      setErr(describeError(e, "Harness 加载失败"));
      setBoot("failed");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // 作战状态自己往前走：只在有在跑会话时轮询
  useEffect(() => {
    if (!cid) return;
    const running = sessions.some((s) => ["analyzing", "announcing", "executing", "delivering"].includes(s.status));
    if (!running) return;
    const t = setInterval(async () => {
      try {
        const d = await api.get<{ sessions: WsSession[] }>(`/war-room/sessions?company_id=${cid}`);
        setSessions(d.sessions || []);
      } catch { /* 下一轮继续 */ }
    }, 8000);
    return () => clearInterval(t);
  }, [cid, sessions]);

  // 窗口重新聚焦时静默刷新员工列表：员工绑定模型发生在右栏 iframe 里，
  // 父壳感知不到——不刷新的话「未绑定模型」警示条会一直挂着误导人。
  useEffect(() => {
    if (boot !== "ready" || !cid) return;
    const refresh = async () => {
      try {
        const rows = await api.get<WsAgent[]>(`/agents?company_id=${cid}`, { silent: true });
        setAgents(Array.isArray(rows) ? rows : []);
      } catch { /* 静默 */ }
    };
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [boot, cid]);

  // 模型就绪探测：没接模型时在状态栏和提示条上说出来
  const checkModel = useCallback(async () => {
    if (!cid) return;
    try {
      const d = await api.get<{ ready?: boolean }>("/models/diagnostics", { silent: true });
      setModelReady(!!d?.ready);
    } catch {
      setModelReady(null);
    }
  }, [cid]);

  useEffect(() => {
    if (boot !== "ready" || !cid) return;
    try {
      setBannerDismissed(localStorage.getItem(`aieos_model_banner:${cid}`) === "1");
    } catch {
      setBannerDismissed(false);
    }
    void checkModel();
    const onFocus = () => void checkModel();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [boot, cid, checkModel]);

  const dismissModelBanner = useCallback(() => {
    setBannerDismissed(true);
    try { localStorage.setItem(`aieos_model_banner:${cid}`, "1"); } catch { /* 隐私模式 */ }
  }, [cid]);

  /* ---------- 布局 ---------- */

  // 存档与量宽都只能在浏览器里做；首屏用默认值，挂载后再对齐
  useEffect(() => {
    setLayout(readLayout());
    const mq = window.matchMedia("(max-width: 899px)");
    const apply = () => {
      const isCompact = mq.matches;
      setCompact(isCompact);
      compactRef.current = isCompact;
      if (isCompact) {
        // 窄屏两侧默认收起；但右栏里正开着一个页面时必须留着
        setLayoutPersist((prev) => ({ ...prev, leftHidden: true, rightHidden: paneRef.current === "out" }), false);
      }
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useLayoutEffect(() => {
    const el = rowRef.current;
    if (!el) return;
    setRowW(el.getBoundingClientRect().width);
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (typeof w === "number") setRowW(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [boot]);

  const setLayoutPersist = useCallback((updater: LayoutState | ((prev: LayoutState) => LayoutState), persist = true) => {
    setLayout((prev) => {
      const next = typeof updater === "function" ? (updater as (p: LayoutState) => LayoutState)(prev) : updater;
      if (persist && !compactRef.current) {
        try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(next)); } catch { /* 隐私模式：仅本次生效 */ }
      }
      return next;
    });
  }, []);

  const toggleLeft = useCallback(() => {
    autoLeft.current = false;
    setLayoutPersist((prev) => ({ ...prev, leftHidden: !prev.leftHidden }));
  }, [setLayoutPersist]);

  const toggleRight = useCallback(() => {
    // 收起右栏时顺带退出展开态，否则会留下「没有右栏却还在展开」的死状态
    setFocus(false);
    setLayoutPersist((prev) => ({ ...prev, rightHidden: !prev.rightHidden }));
  }, [setLayoutPersist]);

  /* ---------- 右栏标签 ---------- */

  const activeApp = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane) || null;
  const activeId = activeApp ? activeApp.id : "out";
  const pageWide = !compact && !!activeApp;
  // 「展开」永远有反应：没有页面时展开 = 产出区吃满中间与右侧
  const focusMode = focus;

  // 页面一打开就把侧栏折起来，页面关掉再放回去；用户自己按过 ⌘B 就不再自动还原
  const leftHiddenRef = useRef(layout.leftHidden);
  leftHiddenRef.current = layout.leftHidden;
  useEffect(() => {
    if (pageWide) {
      if (!layout.leftHidden) {
        autoLeft.current = true;
        setLayoutPersist((prev) => ({ ...prev, leftHidden: true }), false);
      }
    } else if (autoLeft.current) {
      autoLeft.current = false;
      setLayoutPersist((prev) => ({ ...prev, leftHidden: false }), false);
    }
    // 只在「是否有页面」变化时触发；用户手动收起/展开不该被这个 effect 覆盖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageWide]);

  const openAppTab = useCallback((raw: string) => {
    const href = stripEmbed(String(raw || "").trim());
    if (href === "/" || href === "/workspace" || href === "/harness") {
      setPane("out");
      setFocus(false);
      setLayoutPersist((prev) => ({ ...prev, rightHidden: false }), false);
      return;
    }
    if (!href.startsWith("/") || href.startsWith("/workspace") || href.startsWith("/harness")) return;
    const same = apps.find((t) => t.href === href);
    let id = same?.id;
    if (!id) {
      const base = appTab(href);
      id = apps.some((t) => t.id === base.id) ? `${base.id}#${apps.length + 1}` : base.id;
      setApps((prev) => [...prev, { ...base, id: id as string }]);
    }
    setPane(id);
    setFocus(false);
    pushRecentPage(href.split("?")[0]);
    // 页面开在右栏：右栏必须可见；窄屏上顺手把侧栏让出来
    setLayoutPersist((prev) => ({
      ...prev,
      rightHidden: false,
      leftHidden: compactRef.current ? true : prev.leftHidden,
    }), false);
  }, [apps, setLayoutPersist]);

  const closeApp = useCallback((id: string) => {
    frames.current.delete(id);
    const idx = apps.findIndex((t) => t.id === id || t.href === id);
    const rest = apps.filter((t) => t.id !== id && t.href !== id);
    setApps(rest);
    setPane((cur) => (cur === id ? (rest[Math.max(0, idx - 1)]?.id ?? "out") : cur));
  }, [apps]);

  // ⌘K / 侧栏 / 任何页面的入口都走这里
  useEffect(() => {
    return registerAppOpener(openAppTab);
  }, [openAppTab]);

  // iframe 里跳转后，标签标题跟着走
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      const d = e.data as { type?: string; href?: string } | null;
      if (!d || d.type !== "aieos:embed" || typeof d.href !== "string") return;
      const href = stripEmbed(d.href);
      if (!href.startsWith("/")) return;
      const id = Array.from(frames.current.entries())
        .find(([, el]) => el?.contentWindow === e.source)?.[0];
      if (!id) return;
      const nav = navFor(href);
      setApps((prev) => prev.map((t) => (t.id === id
        ? { ...t, href, label: nav?.label || t.label, icon: nav?.icon || t.icon }
        : t)));
      pushRecentPage(href.split("?")[0]);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // 焦点在 iframe 里时，快捷键由内层转上来
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      const d = e.data as { type?: string; name?: string } | null;
      if (!d || d.type !== "aieos:shortcut") return;
      if (d.name === "palette") openCommandPalette();
      else if (d.name === "left") toggleLeft();
      else if (d.name === "right") toggleRight();
      else if (d.name === "new-chat") setPicking(true);
      else if (d.name === "ceo") window.dispatchEvent(new CustomEvent("aieos:ceo-toggle"));
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [toggleLeft, toggleRight]);

  // 地址栏反映当前看的页面
  useEffect(() => {
    const active = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane);
    const want = active ? `/harness?app=${encodeURIComponent(active.href)}` : "/harness";
    const cur = window.location.pathname + window.location.search;
    if (cur !== want) window.history.replaceState(window.history.state, "", want);
  }, [apps, pane]);

  useEffect(() => {
    try {
      localStorage.setItem(APPS_KEY, JSON.stringify({ hrefs: apps.map((t) => t.href), active: activeId }));
    } catch { /* 隐私模式 */ }
  }, [apps, activeId]);

  // Esc：展开态先回到对话；有页面时关掉页面
  useEffect(() => {
    if (!focus && !activeApp) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (focus) { setFocus(false); return; }
      setPane("out");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focus, activeApp]);

  // ⌘B / ⌘J / ⌘N
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "b") { e.preventDefault(); toggleLeft(); }
      else if (k === "j") { e.preventDefault(); toggleRight(); }
      else if (k === "n") { e.preventDefault(); setPicking(true); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleLeft, toggleRight]);

  /* ---------- 会话 ---------- */

  const pickAgent = useCallback((a: WsAgent) => {
    setAgentId(a.id);
    agentIdRef.current = a.id;
    setView({ mode: "single", id: a.id });
    setPicking(false);
    setPane("out");
    setFocus(false);
    try { localStorage.setItem(AGENT_KEY, a.id); } catch { /* 忽略 */ }
  }, []);

  const switchMode = useCallback((mode: WsMode) => {
    setView((prev) => {
      if (prev.mode === mode && prev.id) return prev;
      const id = mode === "single" ? (agentIdRef.current || agents[0]?.id || "")
        : mode === "group" ? (projects[0]?.id || chatGroups[0]?.id || "")
        : mode === "meeting" ? (meetings[0]?.id || "")
        : (sessions[0]?.id || "");
      return { mode, id };
    });
    setPicking(false);
  }, [agents, projects, meetings, sessions, chatGroups]);

  const ensurePresetGroup = useCallback(async (g: WsChatGroup): Promise<WsChatGroup | null> => {
    const pid = g.preset_id;
    if (!pid || !cid) return null;
    try {
      const d = await api.post<{ group: WsChatGroup; created: { name: string }[] }>(
        `/chat-groups/preset/${pid}/ensure`, { company_id: cid });
      const next = d.group;
      setChatGroups((prev) => [next, ...prev.filter((x) => x.id !== g.id && x.id !== next.id)]);
      setView((v) => (v.mode === "group" && v.id === g.id ? { mode: "group", id: next.id } : v));
      if (d.created?.length) {
        notify("success", `已生成 ${d.created.length} 位员工：${d.created.map((x) => x.name).join("、")}`);
        api.get<WsAgent[]>(`/agents?company_id=${cid}`)
          .then((rows) => setAgents(Array.isArray(rows) ? rows : []))
          .catch(() => { /* 下次加载补齐 */ });
      }
      return next;
    } catch (e) {
      notify("error", describeError(e, "生成员工失败"));
      return null;
    }
  }, [cid]);

  const pickGroup = useCallback(async (g: WsChatGroup) => {
    setPicking(false);
    if (g.id.startsWith("preset:") && g.missing?.length) {
      const next = await ensurePresetGroup(g);
      if (next) setView({ mode: "group", id: next.id });
      return;
    }
    setView({ mode: "group", id: g.id });
  }, [ensurePresetGroup]);

  const openGroupEditor = useCallback(async (g: WsChatGroup) => {
    if (g.id.startsWith("preset:")) {
      const next = await ensurePresetGroup(g);
      if (next) setGroupEditor({ open: true, group: next });
      return;
    }
    setGroupEditor({ open: true, group: g });
  }, [ensurePresetGroup]);

  const saveGroup = useCallback(async (name: string, ids: string[]) => {
    if (!cid || groupSaving) return;
    const editing = groupEditor.group;
    setGroupSaving(true);
    try {
      if (editing) {
        const d = await api.patch<WsChatGroup>(`/chat-groups/${editing.id}`, { name, agent_ids: ids });
        setChatGroups((prev) => prev.map((g) => (g.id === d.id ? d : g)));
        notify("success", "群聊已更新");
      } else {
        const d = await api.post<WsChatGroup>("/chat-groups", { company_id: cid, name, agent_ids: ids });
        setChatGroups((prev) => [d, ...prev]);
        setView({ mode: "group", id: d.id });
        notify("success", "群聊已创建");
      }
      setGroupEditor({ open: false, group: null });
    } catch (e) {
      notify("error", describeError(e, editing ? "更新群聊失败" : "创建群聊失败"));
    } finally {
      setGroupSaving(false);
    }
  }, [cid, groupSaving, groupEditor.group]);

  const deleteGroup = useCallback(async () => {
    const g = groupEditor.group;
    if (!g || groupSaving) return;
    const ok = await confirmAction({
      title: `删除「${g.name}」？`,
      description: "群聊记录会一起删除，员工本身不受影响。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    setGroupSaving(true);
    try {
      await api.del(`/chat-groups/${g.id}`);
      setChatGroups((prev) => prev.filter((x) => x.id !== g.id));
      setView((v) => (v.mode === "group" && v.id === g.id
        ? { mode: "single", id: agentIdRef.current || agents[0]?.id || "" }
        : v));
      setGroupEditor({ open: false, group: null });
      notify("success", "群聊已删除");
    } catch (e) {
      notify("error", describeError(e, "删除群聊失败"));
    } finally {
      setGroupSaving(false);
    }
  }, [groupEditor.group, groupSaving, agents]);

  const pickMeeting = useCallback((m: WsMeeting) => {
    setView({ mode: "meeting", id: m.id });
    setPicking(false);
  }, []);

  const pickSession = useCallback((s: WsSession) => {
    setView({ mode: "war", id: s.id });
    setPicking(false);
  }, []);

  const openView = useCallback((v: WsView) => {
    setView(v);
    setPicking(false);
  }, []);

  const addMeeting = useCallback((m: WsMeeting) => {
    setMeetings((prev) => (prev.some((x) => x.id === m.id) ? prev : [m, ...prev]));
    setView({ mode: "meeting", id: m.id });
  }, []);

  // 演示公司种子（空公司引导，与工作台同一套语义）
  const [seedingDemo, setSeedingDemo] = useState(false);
  const seedDemo = useCallback(async () => {
    if (seedingDemo) return;
    setSeedingDemo(true);
    try {
      const { seedDemoCompany } = await import("@/lib/demo");
      const d = await seedDemoCompany();
      setActiveCompanyId(d.company_id);
      notify("success", d.already_seeded
        ? "演示公司已就绪（此前预置过），已为你切换"
        : "🎭 演示公司已就绪：4 名员工、待验收交付单和示例任务都已备好");
      await load();
    } catch (e) {
      notify("error", describeError(e, "演示公司创建失败"));
    } finally {
      setSeedingDemo(false);
    }
  }, [seedingDemo, load]);

  const logout = useCallback(async () => {
    const ok = await confirmAction({
      title: "退出登录？",
      description: "将清除本地凭据并返回登录页。",
      confirmText: "退出",
      danger: true,
    });
    if (!ok) return;
    try {
      await clearToken();
    } catch (e) {
      notify("warning", `退出时清理本地凭据失败：${describeError(e)}`);
    }
    router.push("/login");
  }, [router]);

  const setFrame = useCallback((id: string, el: HTMLIFrameElement | null) => {
    if (el) frames.current.set(id, el);
    else frames.current.delete(id);
  }, []);

  /* ---------- 宽度派生（与工作台同一套预算算法） ---------- */

  const railHidden = layout.leftHidden;
  const visibleSplitters = (railHidden ? 0 : 1) + (layout.rightHidden ? 0 : 1);
  const sideBudget = rowW > 0
    ? Math.max(0, rowW - CENTER_MIN - visibleSplitters * SPLITTER_W)
    : Number.POSITIVE_INFINITY;
  const maxRight = Number.isFinite(sideBudget)
    ? Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, sideBudget - (railHidden ? 0 : LEFT_MIN)))
    : RIGHT_MAX;
  const rightW = layout.rightHidden ? 0 : Math.min(maxRight, Math.max(RIGHT_MIN, layout.right));
  // 页面模式上限不套 RIGHT_MAX——详情页/表格类内容更需要宽度，中栏可读宽度由 CENTER_MIN 兜底
  const maxPageRight = Number.isFinite(sideBudget)
    ? Math.max(RIGHT_MIN, sideBudget - (railHidden ? 0 : LEFT_MIN))
    : RIGHT_MAX;
  const pageRightW = layout.rightHidden ? 0 : Math.min(maxPageRight, Math.max(RIGHT_MIN,
    layout.pageRight > 0 ? layout.pageRight : (rowW > 0 ? Math.round(rowW / 2) : Math.round(RIGHT_MAX / 2))));
  const effectiveRightW = pageWide ? pageRightW : rightW;
  const maxLeft = Number.isFinite(sideBudget)
    ? Math.min(LEFT_MAX, Math.max(LEFT_MIN, sideBudget - rightW))
    : LEFT_MAX;
  const leftW = railHidden ? 0 : Math.min(LEFT_MAX, Math.max(LEFT_MIN, layout.left));

  const agent = agents.find((a) => a.id === agentId) || null;
  const railActiveChat = !activeApp;
  const railActiveHref = activeApp?.href || "";

  /* ---------- 渲染 ---------- */

  const rail = (
    <nav
      aria-label="Harness 导航"
      className="flex flex-col items-center shrink-0 gap-1 py-2"
      style={{ width: 46, background: "#0b0b0e", borderRight: "1px solid var(--border)" }}
    >
      <button
        type="button"
        onClick={() => { setPane("out"); setFocus(false); }}
        title="AI-EOS · Harness"
        aria-label="回到对话"
        className="w-8 h-8 rounded-lg flex items-center justify-center mb-2 transition-transform hover:scale-105"
        style={{ color: "var(--accent)" }}
      >
        <Icon name="zap" size={19} />
      </button>
      <RailButton
        active={railActiveChat}
        onClick={() => { setPane("out"); setFocus(false); }}
        label="对话"
      >
        <Icon name="message" />
      </RailButton>
      {RAIL_PAGES.map((p) => (
        <RailButton
          key={p.href}
          active={railActiveHref === p.href || railActiveHref.startsWith(p.href + "/")}
          onClick={() => openAppTab(p.href)}
          label={p.label}
        >
          <Icon name={p.icon} />
        </RailButton>
      ))}
      <div className="flex-1" />
      <RailButton onClick={openCommandPalette} label="命令面板 (⌘K)">
        <Icon name="search" size={17} />
      </RailButton>
      <RailButton onClick={openModeChooser} label="切换界面模式">
        <Icon name="swap" size={17} />
      </RailButton>
      <div
        className="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold mt-1 shrink-0"
        style={{ background: "linear-gradient(135deg,var(--ws-acc-solid),var(--ws-acc-deep))", color: "#fff" }}
        title={`${userName} · 退出登录`}
        role="button"
        tabIndex={0}
        onClick={logout}
        onKeyDown={(e) => { if (e.key === "Enter") void logout(); }}
      >
        {userName.slice(0, 1) || "我"}
      </div>
    </nav>
  );

  const sidebar = (
    <div
      className="flex flex-col min-h-0 shrink-0 overflow-hidden"
      style={{ width: SIDEBAR_W, background: "var(--surface)", borderRight: "1px solid var(--border)" }}
    >
      <div className="flex items-center gap-2 px-3 shrink-0" style={{ height: 46, borderBottom: "1px solid var(--border)" }}>
        <span className="text-[12.5px] font-bold tracking-wide">Harness</span>
        <span
          className="text-[8.5px] px-1.5 py-px rounded-full font-semibold"
          style={{ background: "rgba(245,134,46,.14)", color: "var(--accent)", border: "1px solid rgba(245,134,46,.3)" }}
        >
          指挥台
        </span>
        <button
          type="button"
          onClick={toggleLeft}
          title="收起侧栏 (⌘B)"
          aria-label="收起侧栏"
          className="ml-auto w-6 h-6 rounded-md flex items-center justify-center hover:bg-white/[0.06]"
          style={{ color: "var(--muted)" }}
        >
          <Icon name="panel-left" size={14} />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto ws-scroll px-2 py-2">
        <SidebarTitle>数字员工 · 在岗 {agents.length}</SidebarTitle>
        {agents.length === 0 && (
          <p className="text-[11px] px-2 py-1.5" style={{ color: "var(--muted)" }}>
            {noCompany ? "还没有公司" : "还没有数字员工"}
          </p>
        )}
        {agents.map((a) => {
          const st = String(a.status || "").toUpperCase();
          const off = st === "PAUSED" || st === "OFF" || st === "OFFLINE";
          return (
            <button
              key={a.id}
              type="button"
              onClick={() => pickAgent(a)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left transition-colors hover:bg-white/[0.05]"
              style={{
                background: view.mode === "single" && view.id === a.id && !activeApp ? "rgba(245,134,46,.1)" : undefined,
                border: view.mode === "single" && view.id === a.id && !activeApp ? "1px solid rgba(245,134,46,.25)" : "1px solid transparent",
              }}
            >
              <span
                className="w-7 h-7 rounded-lg flex items-center justify-center text-[11px] font-bold shrink-0"
                style={{ background: "linear-gradient(135deg,var(--ws-acc-solid),var(--ws-acc-glow))", color: "#fff" }}
                aria-hidden
              >
                {a.name.slice(0, 1)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[12px] font-medium truncate" style={{ color: "var(--foreground)" }}>{a.name}</span>
                <span className="block text-[10px] truncate" style={{ color: agentModelReady(a) ? "var(--muted)" : "var(--warning)" }}>
                  {agentModelReady(a) ? (a.role || "数字员工") : "⚠ 未绑定模型"}
                </span>
              </span>
              {!agentModelReady(a) && (
                <span className="shrink-0 text-[9px] font-semibold"
                  title="未绑定模型：派活会失败，点员工去绑定"
                  style={{ color: "var(--warning)" }}>⚠</span>
              )}
              <span
                className="w-[7px] h-[7px] rounded-full shrink-0"
                style={{
                  background: off ? "var(--muted)" : "var(--success)",
                  boxShadow: off ? undefined : "0 0 5px rgba(52,211,153,.5)",
                  opacity: off ? 0.5 : 1,
                }}
                aria-label={off ? "离岗" : "在岗"}
              />
            </button>
          );
        })}
        <SidebarTitle>基座能力</SidebarTitle>
        {BASE_PAGES.map((p) => {
          const on = railActiveHref === p.href || railActiveHref.startsWith(p.href + "/");
          return (
            <button
              key={p.href}
              type="button"
              onClick={() => openAppTab(p.href)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left text-[12px] transition-colors hover:bg-white/[0.05]"
              style={{
                color: on ? "var(--accent)" : "var(--muted)",
                background: on ? "rgba(245,134,46,.08)" : undefined,
              }}
            >
              <span className="flex-1 truncate">{p.label}</span>
              {on && <span aria-hidden style={{ color: "var(--accent)" }}>▸</span>}
            </button>
          );
        })}
        <button
          type="button"
          onClick={openAllPages}
          className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left text-[12px] transition-colors hover:bg-white/[0.05]"
          style={{ color: "var(--muted)" }}
        >
          <span className="flex-1 truncate">全部页面…</span>
        </button>
      </div>
      <div className="px-3 py-2.5 shrink-0" style={{ borderTop: "1px solid var(--border)" }}>
        <p className="text-[11px] font-medium truncate" style={{ color: "var(--foreground)" }}>
          {companyName || (noCompany ? "尚未创建公司" : "…")}
        </p>
        <p className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{userName}</p>
      </div>
    </div>
  );

  const panel = (
    <WorkspacePanel
      outTab={outTab}
      onOutTab={setOutTab}
      agent={agent}
      cid={cid}
      apps={apps}
      active={activeId}
      onActivate={setPane}
      onClose={closeApp}
      onOpenPicker={openCommandPalette}
      onCollapse={toggleRight}
      focus={focusMode}
      onToggleFocus={() => setFocus((v) => !v)}
      onFrame={setFrame}
      compact={compact}
      showPicker
    />
  );

  const center = picking || (view.mode === "single" && !agent) ? (
    <HarnessPicker
      agents={agents}
      picking={picking}
      onPick={pickAgent}
      onCancel={() => setPicking(false)}
      noCompany={noCompany}
      onDemoSeed={seedDemo}
      demoBusy={seedingDemo}
      onGoToCompanies={() => openAppTab("/companies")}
      leftHidden={railHidden}
      outHidden={layout.rightHidden}
      onExpandLeft={toggleLeft}
      onToggleOut={toggleRight}
    />
  ) : (
    <WorkspaceChat
      view={view}
      agent={agent}
      agents={agents}
      projects={projects}
      meetings={meetings}
      chatGroups={chatGroups}
      sessions={sessions}
      cid={cid}
      onMode={switchMode}
      onOpenView={openView}
      onMeetingCreated={addMeeting}
      onPickAgent={pickAgent}
      onOpenApp={openAppTab}
      onOpenTrace={() => {
        setOutTab("trace");
        // 产出区若被页面标签盖住，切回「产出」——否则用户点了看不到任何反应
        setPane("out");
        setFocus(false);
        setLayoutPersist((prev) => ({ ...prev, rightHidden: false }), false);
      }}
      onEnsureGroup={ensurePresetGroup}
      onEditGroup={openGroupEditor}
      onCollapseOut={toggleRight}
      onExpandLeft={toggleLeft}
      outHidden={layout.rightHidden}
      leftHidden={railHidden}
    />
  );

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden" style={HARNESS_THEME}>
      {boot === "ready" && modelReady === false && !bannerDismissed && (
        <ModelBanner onOpen={() => openAppTab("/models")} onDismiss={dismissModelBanner} />
      )}
      <div className="flex flex-1 min-h-0">
        {rail}
        {boot !== "ready" ? (
          <div className="flex-1 min-w-0 flex flex-col" style={{ background: "var(--background)" }}>
            <BootState state={boot} err={err} onRetry={load} />
          </div>
        ) : compact ? (
          <>
            <div className="flex-1 min-w-0">{center}</div>
            {!railHidden && (
              <>
                <div className="fixed inset-0 z-20 bg-black/55" onClick={toggleLeft} aria-hidden />
                <aside className="fixed inset-y-0 left-[46px] z-30 w-[260px] max-w-[80vw] shadow-2xl"
                  style={{ borderRight: "1px solid var(--border)", background: "var(--surface)" }}>
                  {sidebar}
                </aside>
              </>
            )}
            {!layout.rightHidden && (
              <>
                <div className="fixed inset-0 z-20 bg-black/55" onClick={toggleRight} aria-hidden />
                <aside className={`fixed z-30 shadow-2xl ${activeApp
                  ? "inset-y-0 right-0 left-[46px]"
                  : "inset-y-0 right-0 w-[86vw] max-w-[420px]"}`}
                  style={{ borderLeft: "1px solid var(--border)", background: "var(--surface)" }}>
                  {panel}
                </aside>
              </>
            )}
          </>
        ) : (
          <>
            {!railHidden && (
              <>
                {/* 分隔线自己画线，这一侧不再加边框 */}
                <aside className="shrink-0 min-w-0 overflow-hidden" style={{ width: leftW }}>{sidebar}</aside>
                <Splitter
                  side="left"
                  label="调整侧栏宽度"
                  value={leftW}
                  min={LEFT_MIN}
                  max={maxLeft}
                  onChange={(v) => setLayoutPersist((p) => ({ ...p, left: v }))}
                  onReset={() => setLayoutPersist((p) => ({ ...p, left: DEFAULT_LAYOUT.left }))}
                />
              </>
            )}

            {/* 展开态对话挂起不卸载（hidden），iframe 标签与草稿都保得住 */}
            <div className={`${focusMode ? "hidden" : "flex-1"} min-w-0`} style={{ background: "var(--background)" }}>
              {center}
            </div>

            {!layout.rightHidden && (
              <>
                {!focusMode && (
                  <Splitter
                    side="right"
                    label={pageWide ? "调整页面宽度" : "调整右栏宽度"}
                    value={pageWide ? pageRightW : rightW}
                    min={pageWide ? PAGE_RIGHT_MIN : RIGHT_MIN}
                    max={pageWide ? maxPageRight : maxRight}
                    onChange={(v) => setLayoutPersist((p) => (pageWide ? { ...p, pageRight: v } : { ...p, right: v }))}
                    onReset={() => setLayoutPersist((p) => (pageWide ? { ...p, pageRight: 0 } : { ...p, right: DEFAULT_LAYOUT.right }))}
                  />
                )}
                {/* 面板始终挂在同一个树位：展开/收起只改宽度，iframe 不重载 */}
                <div
                  className={`shrink-0 min-w-0 overflow-hidden ${focusMode ? "flex-1" : ""}`}
                  style={{
                    width: focusMode ? undefined : effectiveRightW,
                    background: "var(--surface)",
                    borderLeft: "1px solid var(--border)",
                  }}
                >
                  {panel}
                </div>
              </>
            )}
            {layout.rightHidden && focusMode && (
              /* 兜底：理论上 toggleRight 会先退展开态，这里防手速竞争 */
              <div className="flex-1 min-w-0" style={{ background: "var(--surface)" }}>{panel}</div>
            )}
          </>
        )}
      </div>
      <StatusBar
        companyName={companyName || (noCompany ? "尚未创建公司" : "")}
        agents={agents.length}
        modelReady={modelReady}
        activeLabel={focusMode ? `${activeApp ? activeApp.label : "产出"} · 展开态` : (activeApp ? activeApp.label : "对话")}
        onOpenPalette={openCommandPalette}
      />
      <FirstRunTour />
      <GroupEditor
        open={groupEditor.open}
        group={groupEditor.group}
        agents={agents}
        busy={groupSaving}
        onClose={() => { if (!groupSaving) setGroupEditor({ open: false, group: null }); }}
        onSave={saveGroup}
        onDelete={deleteGroup}
      />
    </div>
  );
}

/* ---------------- 活动栏按钮 ---------------- */

function RailButton({ active, onClick, label, children }: {
  active?: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-current={active ? "true" : undefined}
      className="relative w-[34px] h-[34px] rounded-lg flex items-center justify-center transition-colors shrink-0"
      style={{
        color: active ? "var(--accent)" : "var(--muted)",
        background: active ? "rgba(245,134,46,.1)" : undefined,
      }}
      onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = "rgba(255,255,255,.06)"; }}
      onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = ""; }}
    >
      {active && (
        <span
          className="absolute left-[-6px] top-[7px] bottom-[7px] w-[2px] rounded-full"
          style={{ background: "var(--accent)", boxShadow: "0 0 6px rgba(245,134,46,.6)" }}
          aria-hidden
        />
      )}
      {children}
    </button>
  );
}

function SidebarTitle({ children }: { children: React.ReactNode }) {
  return (
    <p
      className="px-2 pt-3 pb-1.5 text-[9.5px] font-bold uppercase"
      style={{ color: "var(--muted)", letterSpacing: "1.4px" }}
    >
      {children}
    </p>
  );
}

/* ---------------- 状态栏 ---------------- */

function StatusBar({ companyName, agents, modelReady, activeLabel, onOpenPalette }: {
  companyName: string;
  agents: number;
  modelReady: boolean | null;
  activeLabel: string;
  onOpenPalette: () => void;
}) {
  return (
    <footer
      className="flex items-center gap-4 px-3 shrink-0"
      style={{
        height: 26,
        background: "#0b0b0e",
        borderTop: "1px solid var(--border)",
        color: "var(--muted)",
        fontSize: 10.5,
        fontFamily: '"SF Mono", ui-monospace, Menlo, monospace',
      }}
    >
      <span
        className="w-[6px] h-[6px] rounded-full shrink-0"
        style={{ background: "var(--success)", boxShadow: "0 0 6px rgba(52,211,153,.6)" }}
        aria-hidden
      />
      {companyName && <span className="truncate max-w-[220px]" style={{ color: "var(--foreground)" }}>{companyName}</span>}
      <span>{agents} 名员工</span>
      {modelReady === false ? (
        <span style={{ color: "var(--warning)" }}>⚠ 模型未接入</span>
      ) : modelReady === true ? (
        <span>模型就绪</span>
      ) : null}
      <span className="truncate">当前：{activeLabel}</span>
      <span className="ml-auto flex items-center gap-4">
        <span style={{ color: "var(--accent)" }}>Harness</span>
        <button type="button" onClick={onOpenPalette} className="hover:underline" style={{ color: "var(--muted)" }}>
          ⌘K 命令面板
        </button>
      </span>
    </footer>
  );
}

/* ---------------- 挑人 / 空态 ---------------- */

function HarnessPicker({ agents, picking, onPick, onCancel, noCompany, onDemoSeed, demoBusy,
                         onGoToCompanies, leftHidden, outHidden, onExpandLeft, onToggleOut }: {
  agents: WsAgent[];
  picking: boolean;
  onPick: (a: WsAgent) => void;
  onCancel: () => void;
  noCompany: boolean;
  onDemoSeed: () => void;
  demoBusy: boolean;
  onGoToCompanies: () => void;
  leftHidden: boolean;
  outHidden: boolean;
  onExpandLeft: () => void;
  onToggleOut: () => void;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--background)" }}>
      <div className="flex items-center gap-2.5 px-4 shrink-0" style={{ height: 52, borderBottom: "1px solid var(--border)" }}>
        {leftHidden && (
          <button type="button" onClick={onExpandLeft} title="展开侧栏 ⌘B" aria-label="展开侧栏"
            className="w-8 h-8 -ml-1 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <Icon name="panel-left" size={15} />
          </button>
        )}
        <h1 className="text-sm font-semibold">{picking ? "新对话" : "开始工作"}</h1>
        <p className="text-[10px]" style={{ color: "var(--muted)" }}>选一个员工，直接下指令</p>
        <div className="ml-auto flex items-center gap-1">
          {picking && (
            <button type="button" onClick={onCancel}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              取消
            </button>
          )}
          <button type="button" onClick={onToggleOut} title={outHidden ? "展开右栏 ⌘J" : "收起右栏 ⌘J"}
            aria-label={outHidden ? "展开右栏" : "收起右栏"}
            className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <Icon name="panel-right" size={15} />
          </button>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto ws-scroll p-6">
        {!agents.length ? (
          <div className="text-center py-10">
            {noCompany ? (
              <>
                <p className="text-sm">还没有公司</p>
                <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                  30 秒预置一家带员工、待验收交付单和示例任务的演示公司，直接开逛。
                </p>
                <div className="mt-3 flex flex-col items-center justify-center gap-2">
                  <button type="button" onClick={onDemoSeed} disabled={demoBusy}
                    className="btn-primary inline-flex items-center gap-2 disabled:opacity-50">
                    {demoBusy ? "正在预置演示公司…" : "🎭 30 秒演示：预置一家能直接用的公司"}
                  </button>
                  <button type="button" onClick={onGoToCompanies}
                    className="text-xs underline" style={{ color: "var(--muted)" }}>
                    不用演示，我自己从头建一家
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm">还没有数字员工</p>
                <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>先招一个人，才有人接你的活。</p>
                <div className="mt-3 flex items-center justify-center gap-2">
                  <button type="button" onClick={onGoToCompanies} className="btn-primary">去招人</button>
                </div>
              </>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2 max-w-3xl">
            {agents.map((a) => (
              <button key={a.id} type="button" onClick={() => onPick(a)}
                className="text-left rounded-xl p-3 transition-colors hover:border-[color:var(--accent)]/40"
                style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                <div className="flex items-center gap-2">
                  <span className="w-7 h-7 rounded-lg flex items-center justify-center text-[11px] font-semibold shrink-0"
                    style={{ background: "linear-gradient(135deg,var(--ws-acc-solid),var(--ws-acc-glow))", color: "#fff" }} aria-hidden>
                    {a.name.slice(0, 1)}
                  </span>
                  <span className="text-xs font-medium truncate">{a.name}</span>
                </div>
                <p className="text-[10px] mt-1.5 truncate" style={{ color: "var(--muted)" }}>
                  {a.role || "数字员工"}
                </p>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------- 模型提示条 / 启动状态 ---------------- */

function ModelBanner({ onOpen, onDismiss }: { onOpen: () => void; onDismiss: () => void }) {
  return (
    <div
      className="flex items-center gap-3 px-4 py-2 text-[12px] shrink-0"
      style={{
        background: "rgba(251,191,36,0.09)",
        borderBottom: "1px solid rgba(251,191,36,0.32)",
        color: "#fde68a",
      }}
      role="status"
    >
      <span className="shrink-0" aria-hidden>⚠️</span>
      <span className="min-w-0 leading-snug">
        还没有接入大模型，AI 员工接任务会提示「尚未绑定任何模型」。
      </span>
      <button
        type="button"
        onClick={onOpen}
        className="ml-auto shrink-0 rounded-lg px-2.5 py-1 text-[11.5px] font-medium"
        style={{
          background: "rgba(251,191,36,0.18)",
          border: "1px solid rgba(251,191,36,0.45)",
          color: "#fde68a",
        }}
      >
        去接入 →
      </button>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 px-1.5 py-0.5 rounded text-[13px] leading-none"
        style={{ color: "rgba(253,230,138,0.75)" }}
        aria-label="关闭提示"
      >
        ✕
      </button>
    </div>
  );
}

function BootState({ state, err, onRetry }: {
  state: "loading" | "failed";
  err: string;
  onRetry: () => void;
}) {
  if (state === "loading") {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3">
        <span className="w-5 h-5 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} aria-hidden />
        <p className="text-xs" style={{ color: "var(--muted)" }}>正在准备你的指挥台…</p>
      </div>
    );
  }
  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="card card-static max-w-sm w-full text-center space-y-2.5">
        <div className="text-xl" aria-hidden>⚠️</div>
        <h2 className="text-sm font-medium">指挥台没能加载</h2>
        <p className="text-xs" style={{ color: "var(--muted)" }}>{err || "请稍后重试"}</p>
        <button type="button" onClick={onRetry} className="btn-primary">重试</button>
      </div>
    </div>
  );
}
