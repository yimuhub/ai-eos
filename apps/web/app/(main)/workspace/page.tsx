"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, clearToken, describeError, getActiveCompanyId } from "@/lib/api";
import { notify } from "@/components/Toast";
import { openCommandPalette, pushRecentPage } from "@/components/CommandPalette";
import { navFor, openApp, registerAppOpener, stripEmbed, type AppTab } from "@/lib/apps";
import Splitter from "@/components/workspace/Splitter";
import WorkspaceChat from "@/components/workspace/WorkspaceChat";
import WorkspacePanel from "@/components/workspace/WorkspacePanel";
import WorkspaceSidebar from "@/components/workspace/WorkspaceSidebar";
import type { OutTab } from "@/components/workspace/WorkspaceOutput";
import type { WsAgent, WsSession } from "@/lib/workspace";

/**
 * 工作台（方案 B）：左「人与会话」 / 中「一句话指挥」 / 右「产出」。
 *
 * 三栏宽度都归这里管：分隔线只负责把新数值报上来，这里的 clamp 保证
 * 中间永远 ≥ CENTER_MIN、两侧不超出各自范围——否则拖到底会把聊天框压成一条缝，
 * 而且刷新后还得记住上次的位置。
 *
 * 窄屏（<900px，手机通过局域网访问）不硬塞三栏：两侧变成抽屉，中间独占。
 */

const LAYOUT_KEY = "aieos_workspace_layout";
const AGENT_KEY = "aieos_workspace_agent";
const APPS_KEY = "aieos_workspace_apps";

const LEFT_MIN = 208;
const LEFT_MAX = 380;
const RIGHT_MIN = 360;
const RIGHT_MAX = 760;
const CENTER_MIN = 420;
const SPLITTER_W = 7;

const DEFAULT_LAYOUT = { left: 252, right: 540, leftHidden: false, rightHidden: false };
type LayoutState = typeof DEFAULT_LAYOUT;

function readLayout(): LayoutState {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const d = JSON.parse(raw) || {};
    return {
      left: Number.isFinite(d.left) ? d.left : DEFAULT_LAYOUT.left,
      right: Number.isFinite(d.right) ? d.right : DEFAULT_LAYOUT.right,
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
        // 两个标签各自跳进同一页面是可能的，存档里会出现重复地址
        .filter((h: string) => (seen.has(h) ? false : (seen.add(h), true)))
        .slice(0, 8)
        .map((h: string) => appTab(h));
    }
    if (typeof d?.active === "string") savedActive = d.active;
  } catch {
    /* 存档损坏就当作没开过标签，不值得打断启动 */
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

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v));
}

export default function WorkspacePage() {
  const router = useRouter();
  const rowRef = useRef<HTMLDivElement>(null);
  const compactRef = useRef(false);

  const [boot, setBoot] = useState<"loading" | "ready" | "failed" | "empty">("loading");
  const [err, setErr] = useState("");
  const [cid, setCid] = useState("");
  const [userName, setUserName] = useState("我");
  const [agents, setAgents] = useState<WsAgent[]>([]);
  const [sessions, setSessions] = useState<WsSession[]>([]);
  const [agentId, setAgentId] = useState("");
  const [outTab, setOutTab] = useState<OutTab>("trace");
  const [picking, setPicking] = useState(false);
  const [compact, setCompact] = useState(false);
  const [layout, setLayout] = useState<LayoutState>(DEFAULT_LAYOUT);
  const [rowW, setRowW] = useState(0);
  // 右栏标签：产出固定第一个，其余是打开的功能页面
  // 恢复只在首帧算一次——读 localStorage + 解析 ?app= 不该在两次渲染里给不同答案
  const booted = useRef<ReturnType<typeof bootApps> | null>(null);
  if (!booted.current) booted.current = bootApps();
  const [apps, setApps] = useState<AppTab[]>(booted.current.apps);
  const [pane, setPane] = useState<string>(booted.current.pane);
  const [focus, setFocus] = useState(false);
  const frames = useRef<Map<string, HTMLIFrameElement>>(new Map());
  // 媒体查询回调里要用到当前激活页，但那个 effect 只在挂载时建一次监听
  const paneRef = useRef(pane);
  paneRef.current = pane;

  const setLayoutPersist = useCallback((updater: LayoutState | ((prev: LayoutState) => LayoutState), persist = true) => {
    setLayout((prev) => {
      const next = typeof updater === "function" ? (updater as (p: LayoutState) => LayoutState)(prev) : updater;
      if (persist && !compactRef.current) {
        try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(next)); } catch { /* 隐私模式：仅本次生效 */ }
      }
      return next;
    });
  }, []);

  /* ---------- 数据 ---------- */

  const load = useCallback(async () => {
    setBoot("loading");
    setErr("");
    try {
      const me = await api.get<{ display_name?: string | null; username?: string | null; email?: string | null }>("/auth/me");
      setUserName(me.display_name || me.username || (me.email ? me.email.split("@")[0] : "") || "我");

      const companies = await api.get<{ id: string; name: string }[]>("/companies");
      if (!companies.length) { setBoot("empty"); return; }
      const active = getActiveCompanyId();
      const company = companies.find((c) => c.id === active) || companies[0];
      setCid(company.id);

      const [agentRows, wr] = await Promise.all([
        api.get<WsAgent[]>(`/agents?company_id=${company.id}`),
        api.get<{ sessions: WsSession[] }>(`/war-room/sessions?company_id=${company.id}`)
          .catch(() => ({ sessions: [] as WsSession[] })),
      ]);
      setAgents(Array.isArray(agentRows) ? agentRows : []);
      setSessions(wr.sessions || []);

      setAgentId((prev) => {
        if (prev && agentRows.some((a) => a.id === prev)) return prev;
        let saved = "";
        try { saved = localStorage.getItem(AGENT_KEY) || ""; } catch { /* 忽略 */ }
        if (saved && agentRows.some((a) => a.id === saved)) return saved;
        return agentRows[0]?.id || "";
      });
      setBoot("ready");
    } catch (e) {
      setErr(describeError(e, "工作台加载失败"));
      setBoot("failed");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // 存档与量宽都只能在浏览器里做；首屏用默认值，挂载后再对齐
  useEffect(() => {
    setLayout(readLayout());
    const mq = window.matchMedia("(max-width: 899px)");
    const apply = () => {
      const isCompact = mq.matches;
      setCompact(isCompact);
      compactRef.current = isCompact;
      if (isCompact) {
        // 窄屏两侧默认收起；但右栏里正开着一个页面时必须留着，
        // 否则窄屏刷新会"标签还在、页面没了"，用户以为丢了工作。
        // 不写盘，回到宽屏时保留用户原来的三栏布局。
        setLayoutPersist((prev) => ({
          ...prev,
          leftHidden: true,
          rightHidden: paneRef.current === "out",
        }), false);
      }
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [setLayoutPersist]);

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

  // 作战状态会自己往前走：只在真的有在跑的会话时轮询，避免空转
  useEffect(() => {
    if (!cid) return;
    const running = sessions.some((s) => ["analyzing", "announcing", "executing", "delivering"].includes(s.status));
    if (!running) return;
    const t = setInterval(async () => {
      try {
        const d = await api.get<{ sessions: WsSession[] }>(`/war-room/sessions?company_id=${cid}`);
        setSessions(d.sessions || []);
      } catch { /* 短暂失败不打扰用户，下一轮继续 */ }
    }, 8000);
    return () => clearInterval(t);
  }, [cid, sessions]);

  /* ---------- 宽度 ---------- */

  const visibleSplitters = (layout.leftHidden ? 0 : 1) + (layout.rightHidden ? 0 : 1);
  const sideBudget = rowW > 0
    ? Math.max(0, rowW - CENTER_MIN - visibleSplitters * SPLITTER_W)
    : Number.POSITIVE_INFINITY;
  const maxRight = Number.isFinite(sideBudget)
    ? Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, sideBudget - (layout.leftHidden ? 0 : LEFT_MIN)))
    : RIGHT_MAX;
  const rightW = layout.rightHidden ? 0 : clamp(layout.right, RIGHT_MIN, maxRight);
  const maxLeft = Number.isFinite(sideBudget)
    ? Math.min(LEFT_MAX, Math.max(LEFT_MIN, sideBudget - rightW))
    : LEFT_MAX;
  const leftW = layout.leftHidden ? 0 : clamp(layout.left, LEFT_MIN, maxLeft);

  const agent = agents.find((a) => a.id === agentId) || null;

  const pickAgent = useCallback((a: WsAgent) => {
    setAgentId(a.id);
    setPicking(false);
    try { localStorage.setItem(AGENT_KEY, a.id); } catch { /* 忽略 */ }
  }, []);

  const toggleLeft = useCallback(() => {
    setLayoutPersist((prev) => ({ ...prev, leftHidden: !prev.leftHidden }));
  }, [setLayoutPersist]);

  const toggleRight = useCallback(() => {
    setLayoutPersist((prev) => ({ ...prev, rightHidden: !prev.rightHidden }));
    // 收起右栏时顺带退出展开态，否则会留下「没有右栏却还在展开」的死状态
    setFocus(false);
  }, [setLayoutPersist]);

  const resetLayout = useCallback(() => {
    setLayoutPersist((prev) => ({ ...prev, left: DEFAULT_LAYOUT.left, right: DEFAULT_LAYOUT.right }));
  }, [setLayoutPersist]);

  /* ---------- 右栏标签 ---------- */

  const activeApp = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane) || null;
  const activeId = activeApp ? activeApp.id : "out";
  // 只有真的打开了页面，展开才有意义；否则「展开」会把对话收起来却只看到产出
  const focusMode = focus && !!activeApp;

  const openAppTab = useCallback((raw: string) => {
    const href = stripEmbed(String(raw || "").trim());
    // 「工作台」不是可打开的页面，而是回到产出/对话本身
    if (href === "/" || href === "/workspace") {
      setPane("out");
      setFocus(false);
      setLayoutPersist((prev) => ({ ...prev, rightHidden: false }), false);
      return;
    }
    // 工作台自己不能再开进标签，否则会套娃
    if (!href.startsWith("/") || href.startsWith("/workspace")) return;
    // 按"标签现在的地址"去重：某个标签已经跳进详情页时，再点列表页应该新开一个，
    // 而不是把用户带到详情里（浏览器里点两次同一个链接也是这个行为）。
    const same = apps.find((t) => t.href === href);
    let id = same?.id;
    if (!id) {
      const base = appTab(href);
      // id 只在新建标签时生成一次；它与 href 分离，内层跳转不会重挂 iframe
      id = apps.some((t) => t.id === base.id) ? `${base.id}#${apps.length + 1}` : base.id;
      setApps((prev) => [...prev, { ...base, id: id as string }]);
    }
    setPane(id);
    setFocus(false);
    pushRecentPage(href.split("?")[0]);
    // 页面开在右栏：右栏必须可见；窄屏上顺手把左抽屉让出来
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
    // 关掉当前标签就落到左边那个（和浏览器一致），而不是跳回「产出」
    setPane((cur) => (cur === id ? (rest[Math.max(0, idx - 1)]?.id ?? "out") : cur));
  }, [apps]);

  // 左栏、⌘K 以及任何页面的入口都走这里
  useEffect(() => registerAppOpener(openAppTab), [openAppTab]);

  // iframe 里跳转后，标签标题跟着走；对话区不动
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      const d = e.data as { type?: string; href?: string } | null;
      if (!d || d.type !== "aieos:embed" || typeof d.href !== "string") return;
      // 内嵌页面报回来的是带 embed=1 的地址，存标签前先剥掉，否则刷新会一路带着它
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

  // 焦点在 iframe 里时，快捷键由内层转上来（见 (main)/layout 的 EmbedShortcuts）
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== window.location.origin) return;
      const d = e.data as { type?: string; name?: string } | null;
      if (!d || d.type !== "aieos:shortcut") return;
      if (d.name === "palette") openCommandPalette();
      else if (d.name === "left") toggleLeft();
      else if (d.name === "right") toggleRight();
      else if (d.name === "new-chat") setPicking(true);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [toggleLeft, toggleRight]);

  // 地址栏只反映「当前看的是哪个页面」，刷新/分享都落在同一个界面
  useEffect(() => {
    const active = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane);
    const want = active ? `/workspace?app=${encodeURIComponent(active.href)}` : "/workspace";
    const cur = window.location.pathname + window.location.search;
    if (cur !== want) window.history.replaceState(window.history.state, "", want);
  }, [apps, pane]);

  useEffect(() => {
    try {
      localStorage.setItem(APPS_KEY, JSON.stringify({
        hrefs: apps.map((t) => t.href),
        active: activeId,
      }));
    } catch {
      /* 隐私模式下写不了：标签只在本次会话保留 */
    }
  }, [apps, activeId]);

  // 展开状态下 Esc 回到对话
  useEffect(() => {
    if (!focus) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFocus(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focus]);

  // ⌘B / ⌘J / ⌘N：和原型一致的三把钥匙
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

  async function logout() {
    try {
      await clearToken();
    } catch (e) {
      notify("warning", `退出时清理本地凭据失败：${describeError(e)}`);
    }
    router.push("/login");
  }

  const setFrame = useCallback((id: string, el: HTMLIFrameElement | null) => {
    if (el) frames.current.set(id, el);
    else frames.current.delete(id);
  }, []);

  const sidebar = (
    <WorkspaceSidebar
      agents={agents}
      sessions={sessions}
      activeAgentId={agentId}
      onAgent={pickAgent}
      onSession={(s) => openAppTab(`/warroom/${s.id}`)}
      onNewChat={() => setPicking(true)}
      onCollapse={toggleLeft}
      onLogout={logout}
      onOpenApp={openAppTab}
      userName={userName}
    />
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
    />
  );

  const center = picking || !agent ? (
    <NewChat
      agents={agents}
      picking={picking}
      onPick={pickAgent}
      onCancel={() => setPicking(false)}
    />
  ) : (
    <WorkspaceChat
      agent={agent}
      agents={agents}
      cid={cid}
      onPickAgent={pickAgent}
      onOpenTrace={() => { setOutTab("trace"); setLayoutPersist((p) => ({ ...p, rightHidden: false })); }}
      onCollapseOut={toggleRight}
      onExpandLeft={toggleLeft}
      outHidden={layout.rightHidden}
      leftHidden={layout.leftHidden}
    />
  );

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {boot !== "ready" ? (
        <BootState state={boot} err={err} onRetry={load} />
      ) : (
        <div ref={rowRef} className="flex flex-1 min-h-0">
          {compact ? (
            <>
              <div className="flex-1 min-w-0">{center}</div>
              {!layout.leftHidden && (
                <>
                  <div className="fixed inset-0 z-20 bg-black/55" onClick={toggleLeft} aria-hidden />
                  <aside className="fixed inset-y-0 left-0 z-30 w-[280px] max-w-[86vw] border-r shadow-2xl"
                    style={{ borderColor: "var(--border)" }}>
                    {sidebar}
                  </aside>
                </>
              )}
              {!layout.rightHidden && (
                <>
                  <div className="fixed inset-0 z-20 bg-black/55" onClick={toggleRight} aria-hidden />
                  <aside className={`fixed z-30 border-l shadow-2xl ${activeApp
                    ? "inset-0"
                    : "inset-y-0 right-0 w-[86vw] max-w-[420px]"}`}
                    style={{ borderColor: "var(--border)" }}>
                    {panel}
                  </aside>
                </>
              )}
            </>
          ) : (
            <>
              {!layout.leftHidden && (
                <>
                  {/* 分隔线自己画线，这一侧不再加边框，免得两条线挨着 */}
                  <aside className="shrink-0 min-w-0 overflow-hidden" style={{ width: leftW }}>
                    {sidebar}
                  </aside>
                  <Splitter
                    side="left"
                    label="调整左栏宽度"
                    value={leftW}
                    min={LEFT_MIN}
                    max={maxLeft}
                    onChange={(v) => setLayoutPersist((p) => ({ ...p, left: v }))}
                    onReset={resetLayout}
                  />
                </>
              )}

              {focusMode ? (
                // 展开：功能页面吃满中间与右侧，对话仍在后台（不卸载），Esc 回来
                <aside className="flex-1 min-w-0 overflow-hidden">{panel}</aside>
              ) : (
                <>
                  <div className="flex-1 min-w-0">{center}</div>

                  {!layout.rightHidden && (
                    <>
                      <Splitter
                        side="right"
                        label="调整右栏宽度"
                        value={rightW}
                        min={RIGHT_MIN}
                        max={maxRight}
                        onChange={(v) => setLayoutPersist((p) => ({ ...p, right: v }))}
                        onReset={resetLayout}
                      />
                      <aside className="shrink-0 min-w-0 overflow-hidden" style={{ width: rightW }}>
                        {panel}
                      </aside>
                    </>
                  )}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------------- 状态页 ---------------- */

function BootState({ state, err, onRetry }: {
  state: "loading" | "failed" | "empty";
  err: string;
  onRetry: () => void;
}) {
  if (state === "loading") {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3">
        <span className="w-5 h-5 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} aria-hidden />
        <p className="text-xs" style={{ color: "var(--muted)" }}>正在准备你的工作台…</p>
      </div>
    );
  }
  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="card card-static max-w-sm w-full text-center space-y-2.5">
        <div className="text-xl" aria-hidden>{state === "empty" ? "🏢" : "⚠️"}</div>
        <h2 className="text-sm font-medium">
          {state === "empty" ? "还没有公司" : "工作台没能加载"}
        </h2>
        <p className="text-xs" style={{ color: "var(--muted)" }}>
          {state === "empty" ? "先建一家公司，员工和任务都会挂在它下面。" : (err || "请稍后重试")}
        </p>
        {state === "empty" ? (
          <button type="button" onClick={() => openApp("/companies")} className="btn-primary inline-flex">去建公司</button>
        ) : (
          <button type="button" onClick={onRetry} className="btn-primary">重试</button>
        )}
      </div>
    </div>
  );
}

/* ---------------- 新对话：挑人 ---------------- */

function NewChat({ agents, picking, onPick, onCancel }: {
  agents: WsAgent[];
  picking: boolean;
  onPick: (a: WsAgent) => void;
  onCancel: () => void;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--background)" }}>
      <div className="flex items-center gap-2.5 px-4 shrink-0" style={{ height: 52, borderBottom: "1px solid var(--border)" }}>
        <h1 className="text-sm font-semibold">{picking ? "新对话" : "开始工作"}</h1>
        <p className="text-[10px]" style={{ color: "var(--muted)" }}>选一个员工，直接在中间下指令</p>
        {picking && (
          <button type="button" onClick={onCancel}
            className="ml-auto text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
            style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
            取消
          </button>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto ws-scroll p-6">
        {!agents.length ? (
          <div className="text-center py-10">
            <p className="text-sm">还没有数字员工</p>
            <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>先招一个人，才有人接你的活。</p>
            <div className="mt-3 flex items-center justify-center gap-2">
              <button type="button" onClick={() => openApp("/agents")} className="btn-primary">去招人</button>
              <button type="button" onClick={() => openApp("/factory")} className="btn-ghost">让工厂按目标生成团队</button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2 max-w-3xl">
            {agents.map((a) => (
              <button key={a.id} type="button" onClick={() => onPick(a)}
                className="text-left rounded-xl p-3 transition-colors hover:border-indigo-500/40"
                style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>
                <div className="flex items-center gap-2">
                  <span className="w-7 h-7 rounded-lg flex items-center justify-center text-[11px] font-semibold shrink-0"
                    style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
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
