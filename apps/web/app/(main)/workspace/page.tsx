"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, clearToken, describeError, getActiveCompanyId } from "@/lib/api";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";
import { openCommandPalette, pushRecentPage } from "@/lib/palette";
import { navFor, openApp, registerAppOpener, stripEmbed, type AppTab } from "@/lib/apps";
import { useUIMode, readUIMode } from "@/lib/ui-mode";
import Splitter from "@/components/workspace/Splitter";
import WorkspaceChat from "@/components/workspace/WorkspaceChat";
import WorkspacePanel from "@/components/workspace/WorkspacePanel";
import WorkspaceSidebar from "@/components/workspace/WorkspaceSidebar";
import GroupEditor from "@/components/workspace/GroupEditor";
import NotificationBell from "@/components/NotificationBell";
import type { OutTab } from "@/components/workspace/WorkspaceOutput";
import type { WsAgent, WsChatGroup, WsMeeting, WsMode, WsProject, WsSession, WsView } from "@/lib/workspace";

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

const DEFAULT_LAYOUT = { left: 252, right: 540, pageRight: 0, leftHidden: false, rightHidden: false };
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
  // 经典模式下这一页只是「三栏对话」本身，没有右栏标签容器
  const classic = useUIMode() === "classic";
  const rowRef = useRef<HTMLDivElement>(null);
  const compactRef = useRef(false);

  const [boot, setBoot] = useState<"loading" | "ready" | "failed" | "empty">("loading");
  const [err, setErr] = useState("");
  const [cid, setCid] = useState("");
  // 模型是否就绪：false 时显示顶部提示条。null = 还没查出来，不打扰。
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [userName, setUserName] = useState("我");
  const [agents, setAgents] = useState<WsAgent[]>([]);
  const [sessions, setSessions] = useState<WsSession[]>([]);
  const [projects, setProjects] = useState<WsProject[]>([]);
  const [meetings, setMeetings] = useState<WsMeeting[]>([]);
  const [chatGroups, setChatGroups] = useState<WsChatGroup[]>([]);
  // null = 新建；group 非空 = 编辑
  const [groupEditor, setGroupEditor] = useState<{ open: boolean; group: WsChatGroup | null }>({ open: false, group: null });
  const [groupSaving, setGroupSaving] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [view, setView] = useState<WsView>({ mode: "single", id: "" });
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
  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;
  // 「打开页面时顺手折叠左栏」是临时动作：关掉页面自动复原，不写进布局存档
  const autoLeft = useRef(false);

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

      const [agentRows, wr, projectRows, meetingRows, groupRows] = await Promise.all([
        api.get<WsAgent[]>(`/agents?company_id=${company.id}`),
        api.get<{ sessions: WsSession[] }>(`/war-room/sessions?company_id=${company.id}`)
          .catch(() => ({ sessions: [] as WsSession[] })),
        api.get<WsProject[]>(`/projects?company_id=${company.id}`)
          .catch(() => [] as WsProject[]),
        api.get<WsMeeting[]>(`/meetings?company_id=${company.id}`)
          .catch(() => [] as WsMeeting[]),
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
      const chosen = (prevAgent && agentRows.some((a) => a.id === prevAgent)) ? prevAgent
        : (saved && agentRows.some((a) => a.id === saved)) ? saved
        : agentRows[0]?.id || "";
      setAgentId(chosen);
      agentIdRef.current = chosen;
      setView((prev) => {
        const list: { id: string }[] = prev.mode === "group" ? [...projectRows, ...groups]
          : prev.mode === "meeting" ? meetingRows
          : prev.mode === "war" ? (wr.sessions || []) : agentRows;
        if (prev.id && list.some((r) => r.id === prev.id)) return prev;
        return { mode: "single", id: chosen };
      });
      setBoot("ready");
    } catch (e) {
      setErr(describeError(e, "工作台加载失败"));
      setBoot("failed");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /* ---------- 模型就绪提示条 ---------- */
  // 刚装好的系统里一个模型都没有，员工接任务只会回「尚未绑定任何模型」。
  // 这里在进入工作台的第一屏就把缺口说出来，并直达「模型配置」。
  const checkModel = useCallback(async () => {
    if (!cid) return;
    try {
      const d = await api.get<{ ready?: boolean }>("/models/diagnostics", { silent: true });
      setModelReady(!!d?.ready);
    } catch {
      setModelReady(null); // 查不到就不打扰，别把提示条变成新的噪音
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
    // 用户可能正在右栏标签里配模型：切回来时重新确认，配好了提示条自动消失
    const onFocus = () => void checkModel();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [boot, cid, checkModel]);

  const dismissModelBanner = useCallback(() => {
    setBannerDismissed(true);
    try { localStorage.setItem(`aieos_model_banner:${cid}`, "1"); } catch { /* 隐私模式：仅本次生效 */ }
  }, [cid]);

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

  const activeApp = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane) || null;
  const activeId = activeApp ? activeApp.id : "out";
  const pageWide = !classic && !compact && !!activeApp;

  // 经典模式已经有固定左栏，这里再出一列导航就是重复的人两颗脑袋
  const railHidden = classic || layout.leftHidden;
  const visibleSplitters = (railHidden ? 0 : 1) + (layout.rightHidden ? 0 : 1);
  const sideBudget = rowW > 0
    ? Math.max(0, rowW - CENTER_MIN - visibleSplitters * SPLITTER_W)
    : Number.POSITIVE_INFINITY;
  const maxRight = Number.isFinite(sideBudget)
    ? Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, sideBudget - (railHidden ? 0 : LEFT_MIN)))
    : RIGHT_MAX;
  const rightW = layout.rightHidden ? 0 : clamp(layout.right, RIGHT_MIN, maxRight);
  // 打开功能页：右栏默认占窗口一半，左栏让位，中栏仍保留可读宽度；
  // pageRight 是用户拖出来的宽度（持久化），0 = 尚未拖过、用自动半宽。
  // 页面模式上限不套 RIGHT_MAX——详情页/表格类内容更需要宽度，
  // 中栏可读宽度由 maxPageRight 兜底（≥ CENTER_MIN）。
  const maxPageRight = Number.isFinite(sideBudget)
    ? Math.max(RIGHT_MIN, sideBudget - (railHidden ? 0 : LEFT_MIN))
    : RIGHT_MAX;
  const pageRightW = layout.rightHidden ? 0 : clamp(
    layout.pageRight > 0 ? layout.pageRight : (rowW > 0 ? Math.round(rowW / 2) : Math.round(RIGHT_MAX / 2)),
    RIGHT_MIN, maxPageRight);
  const effectiveRightW = pageWide ? pageRightW : rightW;
  const maxLeft = Number.isFinite(sideBudget)
    ? Math.min(LEFT_MAX, Math.max(LEFT_MIN, sideBudget - rightW))
    : LEFT_MAX;
  const leftW = railHidden ? 0 : clamp(layout.left, LEFT_MIN, maxLeft);

  const agent = agents.find((a) => a.id === agentId) || null;

  const pickAgent = useCallback((a: WsAgent) => {
    setAgentId(a.id);
    agentIdRef.current = a.id;
    setView({ mode: "single", id: a.id });
    setPicking(false);
    try { localStorage.setItem(AGENT_KEY, a.id); } catch { /* 忽略 */ }
  }, []);

  const pickProject = useCallback((p: WsProject) => {
    setView({ mode: "group", id: p.id });
    setPicking(false);
  }, []);

  /** 内置分组缺人：按预设生成员工（幂等），并把视图切到生成后的真实群 id。 */
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
          .catch(() => { /* 员工列表下次加载会补齐 */ });
      }
      return next;
    } catch (e) {
      notify("error", describeError(e, "生成员工失败"));
      return null;
    }
  }, [cid]);

  /** 点内置分组：缺人就当场按预设生成，再进群——用户点的是「进群」，不是「看缺几个人」。 */
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
    // 还没落库的内置分组：先补齐员工拿到真实 id，再打开编辑器
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

  // 作战会话不再单独跳页：中栏就是它的对话现场
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

  // 分段切换：同一个模式里没指定对象时，落到最近一个可用的
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

  const toggleLeft = useCallback(() => {
    // 经典模式的左栏由 ClassicLayout 负责，这里没有可开关的列
    if (classic) return;
    autoLeft.current = false;
    setLayoutPersist((prev) => ({ ...prev, leftHidden: !prev.leftHidden }));
  }, [classic, setLayoutPersist]);

  const toggleRight = useCallback(() => {
    setLayoutPersist((prev) => ({ ...prev, rightHidden: !prev.rightHidden }));
    // 收起右栏时顺带退出展开态，否则会留下「没有右栏却还在展开」的死状态
    setFocus(false);
  }, [setLayoutPersist]);

  const resetLayout = useCallback(() => {
    setLayoutPersist((prev) => ({ ...prev, left: DEFAULT_LAYOUT.left, right: DEFAULT_LAYOUT.right }));
  }, [setLayoutPersist]);

  /* ---------- 右栏标签 ---------- */

  // 页面一打开就把左栏折起来，页面关掉再放回去；用户自己按过 ⌘B 就不再自动还原
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

  // 只有真的打开了页面，展开才有意义；否则「展开」会把对话收起来却只看到产出
  const focusMode = focus && !!activeApp;

  const openAppTab = useCallback((raw: string) => {
    const href = stripEmbed(String(raw || "").trim());
    // 经典模式没有标签容器：所有入口都是整页跳转
    if (classic) {
      router.push(href === "/" ? "/dashboard" : href);
      return;
    }
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
  }, [apps, classic, router, setLayoutPersist]);

  const closeApp = useCallback((id: string) => {
    frames.current.delete(id);
    const idx = apps.findIndex((t) => t.id === id || t.href === id);
    const rest = apps.filter((t) => t.id !== id && t.href !== id);
    setApps(rest);
    // 关掉当前标签就落到左边那个（和浏览器一致），而不是跳回「产出」
    setPane((cur) => (cur === id ? (rest[Math.max(0, idx - 1)]?.id ?? "out") : cur));
  }, [apps]);

  // 左栏、⌘K 以及任何页面的入口都走这里
  useEffect(() => {
    if (classic) return;
    return registerAppOpener(openAppTab);
  }, [classic, openAppTab]);

  // 从极客切到经典时页面可能还挂着：把右栏标签收掉，别留下一条不属于这个模式的栏
  useEffect(() => {
    if (!classic) return;
    frames.current.clear();
    setApps([]);
    setPane("out");
    setFocus(false);
    // 经典页只有一个对话：会议/群聊/作战各自有整页入口，这边回到单聊
    setView((v) => (v.mode === "single" ? v : { mode: "single", id: agentIdRef.current || v.id }));
  }, [classic]);

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
      else if (d.name === "ceo") window.dispatchEvent(new CustomEvent("aieos:ceo-toggle"));
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [toggleLeft, toggleRight]);

  // 地址栏只反映「当前看的是哪个页面」，刷新/分享都落在同一个界面
  useEffect(() => {
    if (classic) return;
    const active = apps.find((t) => t.id === pane) || apps.find((t) => t.href === pane);
    const want = active ? `/workspace?app=${encodeURIComponent(active.href)}` : "/workspace";
    const cur = window.location.pathname + window.location.search;
    if (cur !== want) window.history.replaceState(window.history.state, "", want);
  }, [apps, pane, classic]);

  useEffect(() => {
    if (classic) return;
    try {
      localStorage.setItem(APPS_KEY, JSON.stringify({
        hrefs: apps.map((t) => t.href),
        active: activeId,
      }));
    } catch {
      /* 隐私模式下写不了：标签只在本次会话保留 */
    }
  }, [apps, activeId, classic]);

  // Esc：展开态先回到对话；有页面时关掉页面，右栏与左栏一起复原
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
      projects={projects}
      meetings={meetings}
      chatGroups={chatGroups}
      view={view}
      onAgent={pickAgent}
      onProject={pickProject}
      onMeeting={pickMeeting}
      onSession={pickSession}
      onGroup={pickGroup}
      onNewGroup={() => setGroupEditor({ open: true, group: null })}
      onEditGroup={openGroupEditor}
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
      compact={compact || classic}
      showPicker={!classic}
    />
  );

  const center = picking || (view.mode === "single" && !agent) ? (
    <NewChat
      agents={agents}
      picking={picking}
      onPick={pickAgent}
      onCancel={() => setPicking(false)}
      leftHidden={classic ? false : layout.leftHidden}
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
      onEnsureGroup={ensurePresetGroup}
      onEditGroup={openGroupEditor}
      onOpenTrace={() => { setOutTab("trace"); setLayoutPersist((p) => ({ ...p, rightHidden: false })); }}
      onCollapseOut={toggleRight}
      onExpandLeft={toggleLeft}
      outHidden={layout.rightHidden}
      leftHidden={classic ? false : layout.leftHidden}
      classic={classic}
    />
  );

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {boot === "ready" && modelReady === false && !bannerDismissed && (
        <ModelBanner
          onOpen={() => openApp("/models")}
          onDismiss={dismissModelBanner}
        />
      )}
      {boot !== "ready" ? (
        <BootState state={boot} err={err} onRetry={load} />
      ) : (
        <div ref={rowRef} className="flex flex-1 min-h-0">
          {compact ? (
            <>
              <div className="flex-1 min-w-0">{center}</div>
              {!railHidden && (
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
              {!railHidden && (
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
                      {pageWide ? (
                        <Splitter
                          side="right"
                          label="调整页面宽度"
                          value={pageRightW}
                          min={RIGHT_MIN}
                          max={maxPageRight}
                          onChange={(v) => setLayoutPersist((p) => ({ ...p, pageRight: v }))}
                          onReset={() => setLayoutPersist((p) => ({ ...p, pageRight: 0 }))}
                        />
                      ) : (
                        <Splitter
                          side="right"
                          label="调整右栏宽度"
                          value={rightW}
                          min={RIGHT_MIN}
                          max={maxRight}
                          onChange={(v) => setLayoutPersist((p) => ({ ...p, right: v }))}
                          onReset={resetLayout}
                        />
                      )}
                      <aside className="shrink-0 min-w-0 overflow-hidden transition-[width] duration-200"
                        style={{ width: effectiveRightW }}>
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

/* ---------------- 状态页 ---------------- */

/**
 * 「还没接大模型」提示条。
 *
 * 不放进状态页：公司、员工、会话都是好的，只是员工没有大脑。
 * 做成可关闭的窄条，给一个直达入口，别挡住正在看的东西。
 */
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

function NewChat({ agents, picking, onPick, onCancel,
                    leftHidden, outHidden, onExpandLeft, onToggleOut }: {
  agents: WsAgent[];
  picking: boolean;
  onPick: (a: WsAgent) => void;
  onCancel: () => void;
  leftHidden: boolean;
  outHidden: boolean;
  onExpandLeft: () => void;
  onToggleOut: () => void;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--background)" }}>
      <div className="flex items-center gap-2.5 px-4 shrink-0" style={{ height: 52, borderBottom: "1px solid var(--border)" }}>
        {/* 修复：NewChat 此前没有任何收展入口——两侧面板收起 + 中间停在
            "还没选员工"时，页面上不存在恢复按钮（只剩看不见的 ⌘B/⌘J），
            用户会困在空白中间栏里。补上与 WorkspaceChat 同款的按钮。 */}
        {leftHidden && (
          <button type="button" onClick={onExpandLeft} title="展开左栏 ⌘B" aria-label="展开左栏"
            className="w-8 h-8 -ml-1 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
            </svg>
          </button>
        )}
        <h1 className="text-sm font-semibold">{picking ? "新对话" : "开始工作"}</h1>
        <p className="text-[10px]" style={{ color: "var(--muted)" }}>选一个员工，直接在中间下指令</p>
        <NotificationBell className="ml-auto" />
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
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
            strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" />
          </svg>
        </button>
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
