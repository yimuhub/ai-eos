"use client";

// ⌘K 全局命令面板（页面 / 会话 / 员工 / 常用动作 的统一入口）
//
// ## 为什么不是"把菜单重新分组"
// 系统有 34 个可用页面 + 若干详情页，任何分组方案都只是把"找不到"推迟到第二层。
// 分组能改善**浏览**，但用户在真实场景里是**带着目标来**的——他心里想的是
// "我要配模型""我要看花了多少钱"，而不是"这应该属于哪一组"。
// 让用户按自己的词直接跳到目标，才是 30+ 页面规模下真正省事的方案。
//
// 侧边栏保留分组是为了"逛"，命令面板是为了"到"。
//
// ## 能力
//   - ⌘K / Ctrl+K 唤起（再按一次关闭）
//   - 检索范围：页面标题/说明/别名、会话目标、员工姓名/角色、常用动作
//   - 打开时拉取当前公司的员工与作战会话；失败静默降级，页面命令照常可用
//   - 严格键盘操作：↑↓ 循环、↵ 打开、Esc 关闭、Tab 留在面板内
//   - 焦点管理：打开进搜索框，关闭回到打开前的元素；打开时锁背景滚动
//   - 无障碍：dialog / listbox / option / aria-activedescendant / aria-live 数量播报
//
// 页面清单来自 `lib/nav.ts`——新增页面只改那一处，这里自动生效。
// 拼音检索复用 `nav.ts` 的 `keywords` 别名，不另造拼音表。
//
// ## 加载方式
// 本组件是主界面里最重的一块交互代码，由 `(main)/layout.tsx` 用 `next/dynamic`
// 按需加载；外部入口（侧边栏 / 经典外壳 / 工作台）只从 `lib/palette` 取
// `openCommandPalette` / `openAllPages` 并通过 window 事件唤起，**不要从本文件 import**，
// 否则整块面板会被静态拉回首屏 chunk。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ALL_PAGES, type NavItem } from "@/lib/nav";
import { api, clearToken, getActiveCompanyId } from "@/lib/api";
import { notify } from "@/components/Toast";
import { openApp } from "@/lib/apps";
import { useSwitchMode, useUIMode } from "@/lib/ui-mode";
import { filterCommands, nextIndex } from "@/lib/commandSearch";
import {
  MIN_COMPANY_QUERY_LENGTH,
  searchCompany,
  type CompanySearchGroup,
} from "@/lib/companySearch";
import { SHORTCUT_EVENTS } from "@/lib/shortcuts";
import {
  OPEN_ALL_PAGES_EVENT,
  OPEN_PALETTE_EVENT,
  RECENT_PAGES_KEY,
  pushRecentPage,
  readRecentPages,
} from "@/lib/palette";
import { sessionMeta, type WsSession } from "@/lib/workspace";
import type { Agent } from "@/lib/types";

const RECENT_MAX = 6;

type ResultBase = {
  id: string;
  label: string;
  desc: string;
  icon: string;
  /** 检索别名（英文 / 拼音 / 口语），拼音检索完全复用这份既有数据 */
  keywords?: string[];
  group: string;
};

type PageResult = ResultBase & {
  kind: "page" | "agent" | "session" | "company";
  href: string;
};

type ActionResult = ResultBase & {
  kind: "action";
  run: () => void;
};

type Result = PageResult | ActionResult;

const COMPANY_ICONS: Record<string, string> = {
  task: "◉",
  memory: "🧠",
  knowledge: "❑",
  asset: "🖼",
};

const AGENT_STATUS: Record<string, string> = {
  IDLE: "在岗",
  ACTIVE: "在岗",
  RUNNING: "工作中",
  PAUSED: "已暂停",
  DISABLED: "已停用",
  ERROR: "异常",
};

function pageResult(page: NavItem & { group: string }): PageResult {
  return {
    kind: "page",
    id: `page:${page.href}`,
    label: page.label,
    desc: page.desc,
    icon: page.icon,
    group: page.group,
    href: page.href,
    keywords: page.keywords,
  };
}

/** 可直达页面（"/" 只是工作台别名，不能当成可打开的页面——会套娃） */
const PAGE_RESULTS: PageResult[] = ALL_PAGES
  .filter((page) => page.href !== "/")
  .map(pageResult);

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/** Tab / Shift+Tab 只允许在面板内部循环，不跑回浏览器地址栏。 */
function trapTabKey(event: KeyboardEvent, container: HTMLElement | null) {
  if (!container) return;
  const nodes = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true");
  if (!nodes.length) {
    event.preventDefault();
    return;
  }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (!active || !container.contains(active)) {
    event.preventDefault();
    first.focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/** 打开面板时可选的初始视图。 */
type OpenOptions = { pagesOnly?: boolean };

export default function CommandPalette() {
  const router = useRouter();
  const mode = useUIMode();
  const switchMode = useSwitchMode();
  const [open, setOpen] = useState(false);
  // 「全部页面」模式：空查询时按分组列出所有页面，而不是最近访问/会话/动作
  const [pagesOnly, setPagesOnly] = useState(false);
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [sessions, setSessions] = useState<WsSession[]>([]);
  // 公司资料（任务/记忆/知识/资产）异步检索：只影响独立分组，不阻断本地结果
  const [companyGroups, setCompanyGroups] = useState<CompanySearchGroup[]>([]);
  const [companyStatus, setCompanyStatus] = useState<
    "idle" | "loading" | "error"
  >("idle");
  const companyAbortRef = useRef<AbortController | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  /** 打开面板前焦点在哪：关闭时还回去 */
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const taskTimers = useRef<number[]>([]);

  const openPalette = useCallback((options: OpenOptions = {}) => {
    if (typeof document !== "undefined") {
      const active = document.activeElement;
      returnFocusRef.current = active instanceof HTMLElement ? active : null;
    }
    setPagesOnly(!!options.pagesOnly);
    setQ("");
    setCursor(0);
    setAnnouncement("");
    setOpen(true);
  }, []);

  // 外部入口（侧边栏搜索框、「全部页面」、⌘K 提示）通过 window 事件唤起面板。
  // 面板本身是按需加载的重组件，入口只依赖 `lib/palette` 里的轻量事件名，
  // 这样侧边栏不必静态 import 整个面板。卸载时务必摘掉监听。
  useEffect(() => {
    const onOpen = () => openPalette();
    const onOpenAll = () => openPalette({ pagesOnly: true });
    window.addEventListener(OPEN_PALETTE_EVENT, onOpen);
    window.addEventListener(OPEN_ALL_PAGES_EVENT, onOpenAll);
    return () => {
      window.removeEventListener(OPEN_PALETTE_EVENT, onOpen);
      window.removeEventListener(OPEN_ALL_PAGES_EVENT, onOpenAll);
    };
  }, [openPalette]);

  const close = useCallback(() => {
    setOpen(false);
    setPagesOnly(false);
    setQ("");
    setCursor(0);
    setAnnouncement("");
  }, []);

  // ---------- 打开时拉取当前公司的员工 / 作战会话 ----------
  // 任何一边失败都只让对应分组为空：页面命令与动作必须照常可用。
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const cid = getActiveCompanyId();
    const qs = cid ? `?company_id=${encodeURIComponent(cid)}` : "";

    setAgents([]);
    setSessions([]);

    void Promise.allSettled([
      api.get<Agent[]>(`/agents${qs}`, { signal: controller.signal, silent: true }),
      api.get<{ sessions?: WsSession[] }>(`/war-room/sessions${qs}`, {
        signal: controller.signal,
        silent: true,
      }),
    ]).then(([agentRes, sessionRes]) => {
      if (controller.signal.aborted) return;
      if (agentRes.status === "fulfilled" && Array.isArray(agentRes.value)) {
        setAgents(agentRes.value);
      }
      if (
        sessionRes.status === "fulfilled" &&
        Array.isArray(sessionRes.value?.sessions)
      ) {
        setSessions(sessionRes.value.sessions);
      }
    });

    return () => controller.abort();
  }, [open]);

  // 新建任务：任务页还没挂载时用事件触发新建弹窗，挂载后事件才有监听者。
  // 每个窗口只投递一次：重复投递会在用户刚关掉弹窗后把它重新打开。
  // 极客模式下任务页在右栏 iframe 里，需要等标签出现并加载后再投递。
  const openNewTask = useCallback(() => {
    openApp("/tasks");

    const fire = (target: Window) => {
      try {
        target.dispatchEvent(new CustomEvent(SHORTCUT_EVENTS.newTask));
      } catch {
        /* 跨域窗口无法投递：任务页本身仍然已经打开 */
      }
    };

    // 经典模式 / 任务页已在父窗口：当前窗口就有监听者
    taskTimers.current.push(window.setTimeout(() => fire(window), 350));

    const seen = new WeakSet<HTMLIFrameElement>();
    let polls = 0;
    const scan = () => {
      document.querySelectorAll("iframe").forEach((frame) => {
        if (seen.has(frame)) return;
        const child = frame.contentWindow;
        if (!child) return;
        const doc = frame.contentDocument;
        if (doc?.readyState === "complete" && doc.body?.childElementCount) {
          // 已存在的任务标签：页面早已挂载，稍等一拍再投递
          seen.add(frame);
          taskTimers.current.push(window.setTimeout(() => fire(child), 400));
          return;
        }
        // 新开的标签：load 之后再等 React 订阅生效
        frame.addEventListener("load", () => {
          if (seen.has(frame)) return;
          seen.add(frame);
          taskTimers.current.push(window.setTimeout(() => fire(child), 400));
        }, { once: true });
      });
      polls += 1;
      if (polls < 10) taskTimers.current.push(window.setTimeout(scan, 250));
    };
    scan();
  }, []);

  useEffect(() => {
    const timers = taskTimers.current;
    return () => {
      timers.forEach((id) => window.clearTimeout(id));
    };
  }, []);

  const agentResults = useMemo<PageResult[]>(
    () => agents.map((agent) => {
      const role = agent.role || agent.description || "数字员工";
      const status = AGENT_STATUS[agent.status] || agent.status || "";
      return {
        kind: "agent",
        id: `agent:${agent.id}`,
        label: agent.name,
        desc: status ? `${role} · ${status}` : role,
        icon: "⬡",
        group: "员工",
        href: `/agents/${agent.id}`,
        keywords: [
          "agent",
          "employee",
          "员工",
          "yuangong",
          agent.role || "",
          agent.description || "",
          agent.id,
        ].filter(Boolean),
      };
    }),
    [agents],
  );

  const sessionResults = useMemo<PageResult[]>(
    () => sessions.map((session) => ({
      kind: "session",
      id: `session:${session.id}`,
      label: session.goal || "未命名会话",
      desc: `作战会话 · ${session.leader_name || "未知负责人"} · ${sessionMeta(session.status).label}`,
      icon: "🎯",
      group: "会话",
      href: `/warroom/${session.id}`,
      keywords: [
        "会话",
        "作战",
        "warroom",
        "huiyi",
        session.leader_name || "",
        session.status || "",
        session.goal || "",
      ].filter(Boolean),
    })),
    [sessions],
  );

  const actions = useMemo<ActionResult[]>(
    () => [
      {
        kind: "action",
        id: "action:new-task",
        label: "新建任务",
        desc: "打开任务中心并唤起新建任务",
        icon: "＋",
        group: "常用动作",
        keywords: ["new task", "create task", "jianrenwu", "任务", "派活"],
        run: openNewTask,
      },
      {
        kind: "action",
        id: "action:open-meetings",
        label: "打开会议中心",
        desc: "多个员工开会讨论问题，产出结论",
        icon: "◍",
        group: "常用动作",
        keywords: ["meeting", "meetings", "huiyi", "会议", "讨论"],
        run: () => openApp("/meetings"),
      },
      {
        kind: "action",
        id: "action:open-delivery",
        label: "打开交付中心",
        desc: "按批次查看交付物与验收状态",
        icon: "📦",
        group: "常用动作",
        keywords: ["delivery", "jiaofu", "交付", "验收"],
        run: () => openApp("/delivery"),
      },
      {
        kind: "action",
        id: "action:switch-mode",
        label: mode === "classic" ? "切换到极客模式" : "切换到经典模式",
        desc:
          mode === "classic"
            ? "极客：左中右三栏对话，功能页开在右栏标签，聊天不中断"
            : "经典：左侧固定菜单 + 整页导航，一页一件事",
        icon: "⇄",
        group: "常用动作",
        keywords: ["界面", "模式", "mode", "switch", "classic", "geek"],
        run: () => switchMode(mode === "classic" ? "geek" : "classic"),
      },
      {
        kind: "action",
        id: "action:logout",
        label: "退出登录",
        desc: "清除本地凭据并返回登录页",
        icon: "⇤",
        group: "常用动作",
        keywords: ["logout", "signout", "退出", "登录"],
        run: async () => {
          try {
            await clearToken();
          } catch (e) {
            notify("warning", `退出时清理本地凭据失败：${(e as Error).message || e}`);
          }
          router.push("/login");
        },
      },
      {
        kind: "action",
        id: "action:clear-recent",
        label: "清空「最近访问」记录",
        desc: "只清除本机的访问历史，不影响任何数据",
        icon: "🧹",
        group: "常用动作",
        keywords: ["clear", "recent", "history", "清空", "最近访问"],
        run: () => {
          try {
            localStorage.removeItem(RECENT_PAGES_KEY);
          } catch {
            /* 存储不可写时无副作用 */
          }
          notify("success", "已清空最近访问记录");
        },
      },
    ],
    [mode, openNewTask, router, switchMode],
  );

  const recentResults = useMemo<PageResult[]>(() => {
    if (!open) return [];
    return readRecentPages()
      .map((href) => PAGE_RESULTS.find((page) => page.href === href))
      .filter((page): page is PageResult => !!page);
  }, [open]);

  const allResults = useMemo<Result[]>(
    () => [...PAGE_RESULTS, ...sessionResults, ...agentResults, ...actions],
    [sessionResults, agentResults, actions],
  );

  const query = q.trim();

  // ---------- 公司资料检索：≥2 字后 300ms 防抖，取消旧请求 ----------
  // 失败或加载中只影响「公司资料」分组：本地页面/会话结果必须立即可用。
  useEffect(() => {
    const cid = getActiveCompanyId();
    if (!open || !cid || query.length < MIN_COMPANY_QUERY_LENGTH) {
      companyAbortRef.current?.abort();
      companyAbortRef.current = null;
      setCompanyGroups([]);
      setCompanyStatus("idle");
      return;
    }
    const controller = new AbortController();
    companyAbortRef.current?.abort();
    companyAbortRef.current = controller;
    // 新查询开始即清掉上一轮的公司资料，避免短暂展示与当前关键词无关的条目
    setCompanyGroups([]);
    setCompanyStatus("loading");
    const timer = window.setTimeout(() => {
      void searchCompany({ companyId: cid, query, signal: controller.signal })
        .then((groups) => {
          if (controller.signal.aborted) return;
          setCompanyGroups(groups);
          setCompanyStatus("idle");
        })
        .catch(() => {
          if (controller.signal.aborted) return;
          // 不打断本地结果，只在面板角落提示公司资料暂不可用
          setCompanyGroups([]);
          setCompanyStatus("error");
        });
    }, 300);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      if (companyAbortRef.current === controller) companyAbortRef.current = null;
    };
  }, [open, query]);

  const companyResults = useMemo<PageResult[]>(
    () => companyGroups.flatMap((group) => group.items.map((item) => ({
      kind: "company" as const,
      id: `company:${group.type}:${item.id}`,
      label: item.title,
      desc: item.snippet || group.label,
      icon: COMPANY_ICONS[group.type] || "❑",
      group: "公司资料",
      href: item.href,
      keywords: [group.label, group.type],
    }))),
    [companyGroups],
  );

  // 本地结果：空查询按真实使用顺序；「全部页面」模式列出全部页面；有查询按匹配分排序。
  const localResults = useMemo<Result[]>(() => {
    if (query) return filterCommands(allResults, query, 50);
    if (pagesOnly) return PAGE_RESULTS;
    return [
      ...recentResults,
      ...sessionResults.slice(0, RECENT_MAX),
      ...agentResults.slice(0, 8),
      ...actions,
    ];
  }, [query, allResults, recentResults, sessionResults, agentResults, actions, pagesOnly]);

  // 公司资料固定排在本地页面/会话/员工/动作之后（索引与 sections 的分组一致）
  const results = useMemo<Result[]>(
    () => (query ? [...localResults, ...companyResults] : localResults),
    [query, localResults, companyResults],
  );

  // 查询变化后游标归零，否则会停在一个不存在的位置
  useEffect(() => {
    setCursor(0);
  }, [q, open]);

  // 异步数据到达 / 结果收缩时把游标夹回有效范围
  useEffect(() => {
    setCursor((current) => (results.length ? Math.min(current, results.length - 1) : 0));
  }, [results.length]);

  // 结果数量变化时播报一次（450ms 防抖：连续打字不会每个键都念）
  useEffect(() => {
    if (!open) {
      setAnnouncement("");
      return;
    }
    const timer = window.setTimeout(() => {
      if (query) {
        setAnnouncement(results.length ? `找到 ${results.length} 个结果` : "没有匹配结果");
      } else if (pagesOnly) {
        setAnnouncement(`全部页面共 ${results.length} 项，按分组排列`);
      } else {
        setAnnouncement(`最近访问、会话、员工与常用动作共 ${results.length} 项`);
      }
    }, 450);
    return () => window.clearTimeout(timer);
  }, [open, query, pagesOnly, results.length]);

  // 选中项滚入可视区（键盘上下选择时）
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor, results.length]);

  // 打开时锁背景滚动，关闭/卸载恢复原值
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  // 打开时焦点进搜索框
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => inputRef.current?.focus(), 20);
    return () => window.clearTimeout(timer);
  }, [open]);

  // 关闭时焦点回到打开前的元素
  useEffect(() => {
    if (open) return;
    const target = returnFocusRef.current;
    returnFocusRef.current = null;
    if (!target || !target.isConnected) return;
    const timer = window.setTimeout(() => {
      if (target.isConnected) target.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  // ---------- 全局快捷键 ----------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘K / Ctrl+K：任何时候都能唤起（含输入框内），这是编辑器类工具的通用约定
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (open) close();
        else openPalette();
        return;
      }
      if (!open) return;

      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      // Tab 不跑出面板：只有一个搜索框时就在它自己身上循环
      if (e.key === "Tab") {
        trapTabKey(e, dialogRef.current);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close, openPalette]);

  const go = useCallback(
    (result: Result | undefined) => {
      if (!result) return;
      close();
      if (result.kind === "action") {
        void result.run();
        return;
      }
      // 页面不再整页跳走：工作台会把它开成右栏标签；没有工作台时才回退成跳转
      if (result.kind === "page") pushRecentPage(result.href);
      openApp(result.href);
    },
    [close],
  );

  // 分节展示。空查询按「最近访问 / 会话 / 员工 / 常用动作」分节；
  // 「全部页面」模式按导航分组列出所有页面；有查询时本地命中仍按匹配分排在一起，
  // 公司资料作为独立分组固定排在最后（排在页面/会话之后，且不与本地结果抢排序）。
  //
  // 注意：这个 useMemo 必须放在 `if (!open) return null` 之前。
  // Hook 不能因为"面板关着"就被跳过，否则会破坏 Hook 调用顺序。
  const sections = useMemo<{ title: string; items: { r: Result; idx: number }[] }[]>(() => {
    if (query) {
      const out: { title: string; items: { r: Result; idx: number }[] }[] = [];
      if (localResults.length) {
        out.push({
          title: "搜索结果",
          items: localResults.map((r, idx) => ({ r, idx })),
        });
      }
      if (companyResults.length) {
        out.push({
          title: "公司资料",
          items: companyResults.map((r, offset) => ({
            r,
            idx: localResults.length + offset,
          })),
        });
      }
      return out;
    }
    if (pagesOnly) {
      // 索引必须与 results 一致：这里按原顺序分桶，不重排
      const out: { title: string; items: { r: Result; idx: number }[] }[] = [];
      results.forEach((r, idx) => {
        if (r.kind !== "page") return;
        let section = out.find((item) => item.title === r.group);
        if (!section) {
          section = { title: r.group, items: [] };
          out.push(section);
        }
        section.items.push({ r, idx });
      });
      return out;
    }
    const buckets: { title: string; kind: Result["kind"]; items: { r: Result; idx: number }[] }[] = [
      { title: "最近访问", kind: "page", items: [] },
      { title: "会话", kind: "session", items: [] },
      { title: "员工", kind: "agent", items: [] },
      { title: "常用动作", kind: "action", items: [] },
    ];
    results.forEach((r, idx) => {
      const bucket = buckets.find((item) => item.kind === r.kind);
      bucket?.items.push({ r, idx });
    });
    return buckets
      .filter((bucket) => bucket.items.length)
      .map(({ title, items }) => ({ title, items }));
  }, [query, localResults, companyResults, results, pagesOnly]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-start justify-center pt-[12vh] px-4"
      style={{ background: "rgba(4,4,8,0.6)", backdropFilter: "blur(3px)" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="全局搜索与跳转"
        className="w-full max-w-xl rounded-2xl border shadow-2xl overflow-hidden animate-fade-up"
        style={{ background: "linear-gradient(180deg, #16161f, #101018)", borderColor: "var(--border)" }}
      >
        {/* 结果数量播报：防抖后只念一次，不干扰逐键输入 */}
        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </div>

        {/* 输入区 */}
        <div className="flex items-center gap-3 px-4 py-3 border-b" style={{ borderColor: "var(--border)" }}>
          <span aria-hidden className="text-sm" style={{ color: "var(--muted)" }}>🔍</span>
          <input
            ref={inputRef}
            className="flex-1 bg-transparent outline-none text-sm placeholder:text-[#55555f]"
            style={{ color: "var(--foreground)" }}
            placeholder={
              pagesOnly
                ? "全部页面：按分组浏览，或继续输入搜索页面 / 会话 / 员工 / 操作…"
                : "搜索页面 / 会话 / 员工 / 操作 / 公司资料…  试试「模型」「renwu」「zhzx」"
            }
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((current) => nextIndex(current, results.length, 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((current) => nextIndex(current, results.length, -1));
              } else if (e.key === "Enter") {
                e.preventDefault();
                go(results[cursor]);
              }
            }}
            role="combobox"
            aria-label="搜索页面、会话、员工、操作或公司资料"
            aria-expanded={results.length > 0}
            aria-autocomplete="list"
            aria-controls="cmdk-results"
            aria-activedescendant={results[cursor] ? `cmdk-option-${cursor}` : undefined}
          />
          <kbd
            className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}
          >
            Esc
          </kbd>
        </div>

        {/* 结果区 */}
        {!results.length && (
          <div className="px-4 py-8 text-center text-sm" style={{ color: "var(--muted)" }}>
            {companyStatus === "loading" ? (
              <>正在检索公司资料…</>
            ) : (
              <>
                没有找到「{q}」相关的页面、会话、员工或操作
                <div className="text-xs mt-1.5 opacity-70">
                  {companyStatus === "error"
                    ? "公司资料暂时无法检索；页面 / 会话 / 员工结果不受影响"
                    : "可以试试它的职责，比如「模型」「日志」「花了多少钱」"}
                </div>
              </>
            )}
          </div>
        )}

        <div
          ref={listRef}
          id="cmdk-results"
          role="listbox"
          aria-label="搜索结果"
          hidden={!results.length}
          className="max-h-[52vh] overflow-y-auto py-1.5"
        >
          {sections.map((section, sectionIdx) => {
            const headingId = `cmdk-group-${sectionIdx}`;
            return (
              <div key={section.title} role="group" aria-labelledby={headingId}>
                <div
                  id={headingId}
                  className="px-4 pt-2.5 pb-1 text-[10px] font-semibold tracking-widest uppercase"
                  style={{ color: "#55555f" }}
                >
                  {section.title}
                </div>
                {section.items.map(({ r, idx }) => {
                  const on = idx === cursor;
                  const hint = r.kind === "page"
                    ? r.href
                    : r.kind === "agent"
                      ? "员工"
                      : r.kind === "session"
                        ? "会话"
                        : r.kind === "company"
                          ? "公司资料"
                          : "操作";
                  return (
                    <div
                      key={r.id}
                      id={`cmdk-option-${idx}`}
                      data-idx={idx}
                      role="option"
                      aria-selected={on}
                      title={r.desc}
                      onMouseMove={() => setCursor(idx)}
                      onMouseDown={(e) => {
                        // 用 mousedown 触发：click 会先让输入框失焦，行为不稳定
                        e.preventDefault();
                        go(r);
                      }}
                      className="mx-1.5 px-2.5 py-2 rounded-lg flex items-center gap-3 cursor-pointer"
                      style={
                        on
                          ? { background: "linear-gradient(90deg, rgba(99,102,241,0.20), rgba(99,102,241,0.05))" }
                          : undefined
                      }
                    >
                      <span aria-hidden className="text-sm w-5 text-center shrink-0 opacity-80">{r.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm truncate" style={{ color: on ? "#fff" : "var(--foreground)" }}>
                          {r.label}
                        </div>
                        <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>{r.desc}</div>
                      </div>
                      <span className="shrink-0 text-[10px]" style={{ color: "#55555f" }}>
                        {hint}
                      </span>
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>

        {/* 键盘提示：让用户知道有更快的用法 */}
        <div
          className="flex items-center gap-4 px-4 py-2 border-t text-[11px]"
          style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        >
          <span><kbd className="opacity-80">↑↓</kbd> 选择</span>
          <span><kbd className="opacity-80">↵</kbd> 打开</span>
          <span><kbd className="opacity-80">Esc</kbd> 关闭</span>
          {companyStatus === "loading" && (
            <span className="opacity-70">公司资料检索中…</span>
          )}
          {companyStatus === "error" && (
            <span className="opacity-70">公司资料暂不可用</span>
          )}
          <span className="ml-auto opacity-70">
            {PAGE_RESULTS.length} 个页面可直达 · {mode === "classic" ? "整页打开" : "开在右栏"}
          </span>
        </div>
      </div>
    </div>
  );
}
