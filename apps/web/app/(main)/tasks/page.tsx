"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import Link from "next/link";
import { api, describeError } from "@/lib/api";
import { SHORTCUT_EVENTS, onShortcut } from "@/lib/shortcuts";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import { notify } from "@/components/Toast";
import { TASK_STATUS_ZH, zh } from "@/lib/i18n";
import { timeAgo } from "@/lib/workspace";
import type { Agent, Task as TaskEntity } from "@/lib/types";

/* ============================================================
   任务中心 v2：渐变横幅 · 多维筛选 · 卡片视图 · 生命周期操作
   （与项目页同一设计体系）
   ============================================================ */

// 复用 lib/types 的任务实体，只把列表展示时必定存在的字段收窄为必填。
// 好处：后端一旦把 status/priority 改名，这里编译期就报错。
type Task = Omit<TaskEntity, "status" | "priority" | "assigned_agent_id"> & {
  status: string;
  priority: string;
  assigned_agent_id: string | null;
};

const STATUS_STYLE: Record<string, { label: string; cls: string; dot: string }> = {
  TODO: { label: "待办", cls: "text-[var(--muted)]", dot: "bg-gray-500" },
  PLANNING: { label: "规划中", cls: "text-purple-300", dot: "bg-purple-400" },
  RUNNING: { label: "执行中", cls: "text-cyan-300", dot: "bg-cyan-400" },
  WAITING: { label: "等待中", cls: "text-yellow-300", dot: "bg-yellow-400" },
  PAUSED: { label: "已暂停", cls: "text-amber-300", dot: "bg-amber-400" },
  REVIEW: { label: "评审中", cls: "text-blue-300", dot: "bg-blue-400" },
  COMPLETED: { label: "已完成", cls: "text-emerald-300", dot: "bg-emerald-400" },
  FAILED: { label: "失败", cls: "text-red-300", dot: "bg-red-400" },
  CANCELLED: { label: "已取消", cls: "text-[var(--muted)]", dot: "bg-gray-600" },
};

const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  LOW: { label: "低", cls: "text-[var(--muted)]" },
  NORMAL: { label: "普通", cls: "text-sky-300" },
  HIGH: { label: "高", cls: "text-amber-300" },
  URGENT: { label: "紧急", cls: "text-red-300" },
};

const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

/* ---------- C1 任务收件箱 ---------- */
type InboxItem = {
  id: string;
  task_id: string;
  title: string;
  status: string;
  agent_id: string | null;
  agent_name: string | null;
  updated_at: string | null;
  artifact_id: string | null;
  artifact_status: string | null;
};
type InboxBucket = "in_progress" | "awaiting_acceptance" | "completed" | "failed";
type InboxData = {
  counts: Record<InboxBucket, number>;
  in_progress: InboxItem[];
  awaiting_acceptance: InboxItem[];
  completed: InboxItem[];
  failed: InboxItem[];
};
const INBOX_BUCKETS: { key: InboxBucket; label: string; empty: string }[] = [
  { key: "in_progress", label: "进行中", empty: "现在没有进行中的任务" },
  { key: "awaiting_acceptance", label: "待验收", empty: "现在没有待验收的交付物" },
  { key: "completed", label: "已完成", empty: "还没有已完成的任务" },
  { key: "failed", label: "失败", empty: "没有失败的任务" },
];
const EMPTY_INBOX: InboxData = {
  counts: { in_progress: 0, awaiting_acceptance: 0, completed: 0, failed: 0 },
  in_progress: [], awaiting_acceptance: [], completed: [], failed: [],
};

type Dim = "status" | "priority" | "executor" | "none";

export default function TasksPage() {
  const router = useRouter();
  const cid = useActiveCompanyId();
  const [dim, setDim] = useState<Dim>("status");
  const [dimValue, setDimValue] = useState("全部");
  const [search, setSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  // 中文输入法守卫：组合（选词）期间不回写 state，避免受控 value
  // 每键一更导致候选窗被打断/字符抖动；compositionend 再一次性同步。
  const searchComposing = useRef(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  // C1 收件箱：默认进来先看"现在该我处理什么"，全部任务仍是同一页面的一级视图
  const [view, setView] = useState<"inbox" | "all">("inbox");
  const [bucket, setBucket] = useState<InboxBucket>("awaiting_acceptance");
  const [inbox, setInbox] = useState<InboxData | null>(null);
  const [inboxError, setInboxError] = useState("");
  const [inboxLoading, setInboxLoading] = useState(true);
  // 手动重试计数：自增即重新拉取（不改动 15s 轮询的节奏）
  const [inboxRetry, setInboxRetry] = useState(0);
  // 键盘导航高亮位（j/k 移动，空格多选，回车打开）
  const [kbIndex, setKbIndex] = useState(0);

  // 批量操作
  const [showBatchDelete, setShowBatchDelete] = useState(false);
  const [batchSel, setBatchSel] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  // 新建任务（仿项目中心）
  const [mounted, setMounted] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [ntForm, setNtForm] = useState({ title: "", description: "", priority: "NORMAL" });
  // 创建后是否立即开工（后端按 worker 队列启动，队满则排队稍后自动启动）
  const [ntAutoStart, setNtAutoStart] = useState(true);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [deptOpen, setDeptOpen] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [ntAttach, setNtAttach] = useState<Attachment[]>([]);
  const [confirmTarget, setConfirmTarget] = useState<{ t: Task } | null>(null);
  useEffect(() => { setMounted(true); }, []);

  /* ---------- 数据加载 ----------
   *
   * 改造前：先取公司 → 再取任务 → 再取员工 → 再取部门，四段串行；
   * 而且用的是 `cs[0].id` 这种一次性快照——用户在顶栏切了公司，
   * 这一页仍然显示旧公司的任务（界面在说谎）。
   * 现在交给统一数据层：并发取数、缓存首屏、切公司自动跟随。
   */
  const {
    data: bundle, error: loadError, refresh,
  } = useResource(
    cid ? `tasks:${cid}` : null,
    async () => {
      // 任务列表必须有；员工与部门只用于"按部门选人"，缺失不阻塞主流程
      const [tasks, agents, deptOv] = await Promise.all([
        api.get<Task[]>(`/tasks?company_id=${cid}`),
        api.get<Agent[]>(`/agents?company_id=${cid}`).catch(() => [] as Agent[]),
        api.get<{ departments?: {
          dept_id: string; dept_name: string;
          agents?: { id: string; name: string }[];
        }[] }>(`/growth-center/departments-overview?company_id=${cid}`).catch(() => null),
      ]);
      return {
        tasks: Array.isArray(tasks) ? tasks : [],
        agents: Array.isArray(agents) ? agents.map((a) => ({ id: a.id, name: a.name })) : [],
        deptGroups: (deptOv?.departments || []).map((d) => ({
          dept_id: d.dept_id, dept_name: d.dept_name,
          agents: (d.agents || []).map((a) => ({ id: a.id, name: a.name })),
        })),
      };
    },
  );

  // 派生数据统一 useMemo：`?? []` 每次渲染都是新数组，会让下游 useMemo 每次失效
  const tasks = useMemo<Task[]>(() => bundle?.tasks ?? [], [bundle]);
  const agents = useMemo(() => bundle?.agents ?? [], [bundle]);
  const deptGroups = useMemo(() => bundle?.deptGroups ?? [], [bundle]);

  /** 任务变了 → 重取本页（同时让首页指挥中心的同类数据失效） */
  const load = useCallback(async () => { await refresh(); }, [refresh]);

  /* ---------- C1 收件箱：15s 静默刷新 ----------
   * 只在收件箱视图下轮询；切换视图 / 换公司 / 卸载都会清掉定时器。
   * 静默 = 不置 loading、不清旧数据，失败才提示，避免列表每 15 秒闪一次。
   */
  useEffect(() => {
    if (view !== "inbox") return;
    if (!cid) { setInboxLoading(false); return; }
    let alive = true;
    const pull = async (silent: boolean) => {
      if (!silent) setInboxLoading(true);
      try {
        const data = await api.get<InboxData>(`/tasks/inbox?company_id=${cid}`);
        if (!alive) return;
        setInbox(data);
        setInboxError("");
      } catch (e) {
        if (alive) setInboxError(describeError(e, "收件箱加载失败"));
      } finally {
        if (alive && !silent) setInboxLoading(false);
      }
    };
    void pull(false);
    const timer = setInterval(() => { void pull(true); }, 15_000);
    return () => { alive = false; clearInterval(timer); };
  }, [view, cid, inboxRetry]);

  const inboxData = inbox ?? EMPTY_INBOX;
  const inboxItems = inboxData[bucket];

  /* ---------- 筛选 ---------- */
  const dimOptions = useMemo(() => {
    if (dim === "status")
      return ["全部", ...Object.values(STATUS_STYLE).map((s) => s.label)];
    if (dim === "priority") return ["全部", "低", "普通", "高", "紧急"];
    if (dim === "executor")
      return ["全部", ...agents.map((a) => a.name), "未分配"];
    return ["全部"];
  }, [dim, agents]);

  const filtered = useMemo(() => {
    let list = [...tasks];
    // 排序：执行中 > 等待/暂停 > 待办 > 终态
    const order: Record<string, number> = {
      RUNNING: 0, WAITING: 1, PLANNING: 2, REVIEW: 2,
      PAUSED: 3, TODO: 4, FAILED: 5, CANCELLED: 6, COMPLETED: 7,
    };
    list.sort((a, b) =>
      ((order[a.status] ?? 9) - (order[b.status] ?? 9)) ||
      a.title.localeCompare(b.title, "zh"));
    if (search)
      list = list.filter((t) => t.title.toLowerCase().includes(search.toLowerCase()));
    if (dimValue !== "全部") {
      list = list.filter((t) => {
        if (dim === "status") return STATUS_STYLE[t.status]?.label === dimValue;
        if (dim === "priority") return PRIORITY_META[t.priority]?.label === dimValue;
        if (dim === "executor") {
          const a = agents.find((x) => x.id === t.assigned_agent_id);
          return dimValue === "未分配" ? !t.assigned_agent_id : a?.name === dimValue;
        }
        return true;
      });
    }
    return list;
  }, [tasks, dim, dimValue, search, agents]);

  /* ---------- 任务操作 ---------- */
  async function control(id: string, action: "pause" | "resume" | "cancel" | "start") {
    setBusyId(id);
    const zhLabel: Record<string, string> = {
      pause: "已暂停（进度保留）", resume: "已恢复执行",
      cancel: "已停止（成果保留）", start: "已启动",
    };
    try {
      // 不管返回什么用户都只看到"✅ 已暂停"，这是"系统会说谎"的典型形态。
      // 现在失败的异常会被统一转成可读中文，并如实报错。
      await api.post(`/tasks/${id}/${action}`);
      setMsg(`✅ ${zhLabel[action]}`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
    setBusyId(null);
  }

  /* ---------- 部门化选人（仿项目中心） ---------- */
  const allAgentIds = deptGroups.flatMap((g) => g.agents.map((a) => a.id));
  const isAllPicked = allAgentIds.length > 0 && allAgentIds.every((id) => picked.has(id));
  const toggleAll = () => setPicked(isAllPicked ? new Set() : new Set(allAgentIds));
  const toggleAgent = (id: string) => setPicked((prev) => {
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
  });
  const isDeptAllPicked = (g: { agents: { id: string }[] }) => g.agents.every((a) => picked.has(a.id));
  const toggleDept = (g: { agents: { id: string }[] }) => {
    const ids = g.agents.map((a) => a.id);
    const all = ids.every((id) => picked.has(id));
    setPicked((prev) => {
      const n = new Set(prev);
      ids.forEach((id) => (all ? n.delete(id) : n.add(id)));
      return n;
    });
  };

  /** 新建并分发任务（走 task-dispatch API，按选中人员逐一分发） */
  async function createTask() {
    if (!ntForm.title.trim() || !picked.size) return;
    setCreating(true);
    try {
      const attachNote = ntAttach.length
        ? `\n\n📎 附件：${ntAttach.map((a) => a.name).join("、")}`
        : "";
      // 走统一 api 层：自动注入鉴权与超时，错误也是可读中文。
      // 原实现是裸 fetch + 手工拼 token，且把 `detail` 直接塞进 Error.message，
      // 校验失败时会显示 "[object Object]"。
      const data = await api.post<{
        created_count: number;
        // 旧后端不返回 started_count / queued_count，按 0 兜底
        started_count?: number;
        queued_count?: number;
      }>("/task-dispatch/dispatch", {
        company_id: cid,
        title: ntForm.title.trim(),
        description: (ntForm.description || null)
          ? ntForm.description + attachNote : (attachNote ? attachNote.trim() : null),
        priority: ntForm.priority,
        target_type: "multi_agent_list",
        agent_ids: Array.from(picked),
        auto_start: ntAutoStart,
      });
      if (ntAutoStart) {
        const started = data.started_count ?? 0;
        const queued = data.queued_count ?? 0;
        setMsg(`✅ 已创建 ${data.created_count} 个任务，其中 ${started} 个已开始执行，${queued} 个排队中`);
      } else {
        setMsg(`✅ 已创建任务「${ntForm.title.trim()}」· 分发给 ${data.created_count} 名成员`);
      }
      setShowCreate(false);
      setNtForm({ title: "", description: "", priority: "NORMAL" });
      setNtAutoStart(true);
      setPicked(new Set()); setNtAttach([]);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "创建任务失败")}`); }
    setCreating(false);
  }

  /** 已完成任务 → 可复用模板（automation_definitions, TEST/PAUSED） */
  async function saveAsTemplate(t: Task) {
    setBusyId(t.id);
    try {
      const data = await api.post<{ id: string; name: string }>(
        `/tasks/${t.id}/save-as-template`);
      notify("success", `已存为模板「${data.name}」，可在自动化页复用`);
    } catch (e) {
      notify("error", describeError(e, "存为模板失败"));
    }
    setBusyId(null);
  }

  /** 删除任务（彻底清除）——弹确认框 */
  function requestDelete(t: Task) {
    setConfirmTarget({ t });
  }

  async function executeDelete() {
    if (!confirmTarget) return;
    const t = confirmTarget.t;
    setConfirmTarget(null);
    setBusyId(t.id);
    try {
      const data = await api.post<{ deleted_count: number; failed: string[] }>(
        "/task-dispatch/delete", { task_ids: [t.id] });
      // 原来只要请求没抛异常就提示成功，甚至不看 deleted_count，
      // "删除失败"要和"删除成功"一样明确地说出来。
      if (data.deleted_count === 1) setMsg("🗑 已彻底删除");
      else setMsg(`❌ 删除失败：${JSON.stringify(data.failed)}`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "删除失败")}`); }
    setBusyId(null);
  }

  /* ---------- 批量删除（复用 task-dispatch delete API）---------- */
  async function batchDeleteTasks() {
    if (!batchSel.size) return;
    setBatchDeleting(true);
    try {
      const data = await api.post<{ deleted_count: number; failed: string[] }>(
        "/task-dispatch/delete", { task_ids: Array.from(batchSel) });
      setMsg(`🗑 已彻底删除 ${data.deleted_count} 个任务${data.failed.length ? `，失败 ${data.failed.length}` : ""}`);
      setShowBatchDelete(false);
      setBatchSel(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "批量删除失败")}`); }
    setBatchDeleting(false);
  }

  function toggleBatchSel(id: string) {
    setBatchSel((prev) => {
      const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
    });
  }

  /* ---------- 全局快捷键（n / / j k / 空格 / 回车）----------
   *
   * n：打开新建任务弹窗；/：聚焦搜索框；
   * j/k：在当前筛选列表里移动高亮；空格：批量删除模式下勾选高亮项；
   * 回车：打开高亮任务的详情页。
   * 放在 filtered/toggleBatchSel 定义之后，避免 TDZ。
   */
  useEffect(() => {
    const clampIdx = (i: number) =>
      filtered.length ? Math.min(filtered.length - 1, Math.max(0, i)) : 0;
    const offs = [
      onShortcut(SHORTCUT_EVENTS.newTask, () => setShowCreate(true)),
      onShortcut(SHORTCUT_EVENTS.focusSearch, () => searchRef.current?.focus()),
      onShortcut(SHORTCUT_EVENTS.move, ({ dir }: { dir: 1 | -1 }) =>
        setKbIndex((i) => (view === "all" ? clampIdx(i + dir) : i))),
      onShortcut(SHORTCUT_EVENTS.toggleSelect, () => {
        const t = filtered[clampIdx(kbIndex)];
        if (view === "all" && showBatchDelete && t) toggleBatchSel(t.id);
      }),
      onShortcut(SHORTCUT_EVENTS.openItem, () => {
        const t = filtered[clampIdx(kbIndex)];
        if (view === "all" && t && !showBatchDelete) router.push(`/tasks/${t.id}`);
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [filtered, kbIndex, showBatchDelete, router, view]);

  // 筛选条件变了，高亮位归零，避免停在一个不存在的位置
  useEffect(() => { setKbIndex(0); }, [search, dim, dimValue]);

  // 高亮卡片滚入可视区（j/k 键盘导航时）
  useEffect(() => {
    if (typeof document === "undefined") return;
    document.querySelector(`[data-task-idx="${kbIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [kbIndex]);

  const agentOf = (id: string | null) =>
    id ? (agents.find((a) => a.id === id)?.name ?? "已离职") : null;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头横幅 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative flex items-end justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">任务中心</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              共 {tasks.length} 个任务 · 多维分类 · 批量管理
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* 收件箱 / 全部任务：同一页面切换，不新增一级导航 */}
            <div className="flex items-center rounded-xl p-0.5"
              style={{ background: "rgba(255,255,255,0.05)", border: "1px solid var(--border)" }}>
              {([["inbox", "收件箱"], ["all", "全部任务"]] as const).map(([v, label]) => {
                const on = view === v;
                return (
                  <button key={v} type="button" data-view={v}
                    onClick={() => setView(v)}
                    className={`px-3 py-1 rounded-[10px] text-xs transition-colors ${
                      on ? "bg-indigo-500/25 text-indigo-100 font-medium" : ""}`}
                    style={on ? {} : { color: "var(--muted)" }}>
                    {label}
                    {v === "inbox" && inboxData.counts.awaiting_acceptance > 0 && (
                      <span className="ml-1 text-[10px] px-1.5 py-px rounded-full bg-amber-500/25 text-amber-200 tabular-nums">
                        {inboxData.counts.awaiting_acceptance}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
            {view === "all" && (
              <button onClick={() => { setShowBatchDelete(true); setBatchSel(new Set()); }}
                className="btn-ghost">🗑 批量删除</button>
            )}
            <button onClick={() => setShowCreate(!showCreate)} className="btn-primary">
              ＋ 新建任务
            </button>
          </div>
        </div>
        {/* 概览数字 */}
        <div className="relative mt-5 flex gap-8">
          {[["全部", tasks.length],
            ["执行中", tasks.filter((t) => t.status === "RUNNING").length],
            ["已暂停", tasks.filter((t) => t.status === "PAUSED").length],
            ["已完成", tasks.filter((t) => t.status === "COMPLETED").length]].map(([k, v]) => (
            <div key={k as string}>
              <div className="text-xl font-bold tabular-nums text-gradient">{v}</div>
              <div className="text-[10px] mt-0.5 tracking-wide" style={{ color: "var(--muted)" }}>{k}</div>
            </div>
          ))}
        </div>
      </div>

      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {view === "all" && (<>
      {/* 多维筛选栏 */}
      <div className="card card-static mb-5 flex items-center gap-3 flex-wrap !py-3">
        <span className="text-xs font-medium shrink-0">分类维度：</span>
        {([["status", "按状态"], ["priority", "按优先级"], ["executor", "按执行 Agent"]] as const).map(([v, label]) => (
          <button key={v}
            onClick={() => { setDim(v); setDimValue("全部"); }}
            className={`px-3 py-1 rounded-full text-xs transition-all border ${
              dim === v ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                        : "hover:bg-white/[0.04]"}`}
            style={dim === v ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
            {label}
          </button>
        ))}
        {dim !== "none" && (
          <select value={dimValue} onChange={(e) => setDimValue(e.target.value)}
            className="input w-auto !py-1.5 text-xs ml-auto">
            {dimOptions.map((o) => (<option key={o} value={o}>{o}</option>))}
          </select>
        )}
        <input placeholder="搜索…（按 / 快速聚焦）" value={search} ref={searchRef}
          onCompositionStart={() => { searchComposing.current = true; }}
          onCompositionEnd={(e) => {
            searchComposing.current = false;
            setSearch(e.currentTarget.value);
          }}
          onChange={(e) => { if (!searchComposing.current) setSearch(e.target.value); }}
          className="input w-40 !py-1.5 text-xs" />
      </div>

      {/* 卡片网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-start" data-task-list>
        {filtered.map((t, i) => {
          const st = STATUS_STYLE[t.status] || STATUS_STYLE.TODO;
          const prio = PRIORITY_META[t.priority];
          const grad = AVATAR_GRADS[i % AVATAR_GRADS.length];
          const isBusy = busyId === t.id;
          const inBatch = showBatchDelete && batchSel.has(t.id);
          const executor = agentOf(t.assigned_agent_id);
          const canPause = t.status === "RUNNING";
          const canStop = !["COMPLETED", "CANCELLED"].includes(t.status);

          return (
            <div key={t.id}
              data-task-idx={i}
              className={`card group relative overflow-hidden cursor-pointer flex flex-col ${
                inBatch ? "!border-red-500/60" : ""}`}
              style={{
                ...(inBatch ? { boxShadow: "0 0 0 1px rgba(239,68,68,0.4)" } : {}),
                // j/k 键盘导航的高亮位
                ...(i === kbIndex && !inBatch ? { boxShadow: "0 0 0 1px rgba(99,102,241,0.55)" } : {}),
              }}
              onClick={() => {
                if (showBatchDelete) {
                  toggleBatchSel(t.id);
                } else {
                  router.push(`/tasks/${t.id}`);
                }
              }}>
              <div className="absolute top-0 left-0 w-full h-[2px]"
                style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />

              {/* 批量模式勾选框 */}
              {showBatchDelete && (
                <label className="absolute top-3 right-3 z-10 cursor-pointer"
                  onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={inBatch} onChange={() => toggleBatchSel(t.id)}
                    className="accent-red-500 w-4 h-4" />
                </label>
              )}

              <div className="flex items-start gap-3">
                <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0"
                  style={{
                    background: `linear-gradient(135deg, ${grad})`,
                    boxShadow: `0 4px 14px -6px ${grad.split(",")[0]}66`,
                  }}>
                  ◉
                </div>
                <div className="min-w-0 flex-1 pr-12">
                  <Link href={`/tasks/${t.id}`} onClick={(e) => e.stopPropagation()}
                    className="font-semibold text-sm hover:text-indigo-300 line-clamp-2 leading-snug">
                    {t.title}
                  </Link>
                  <div className="mt-1.5 inline-flex items-center gap-1.5 text-[11px]">
                    <span className={`w-1.5 h-1.5 rounded-full ${st.dot} ${t.status === "RUNNING" ? "pulse-dot" : ""}`} />
                    <span className={st.cls}>{st.label}</span>
                    {prio && (
                      <span className={`ml-1 text-[10px] px-1.5 py-0.5 rounded-md ${prio.cls}`}
                        style={{ background: "rgba(255,255,255,0.04)" }}>
                        {prio.label}优先级
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* 进度：读后端任务模型的 progress(0.0-1.0)/current_stage；
                  旧版本后端不返回该字段时整块隐藏 */}
              {typeof t.progress === "number" && (
                <div className="mt-2">
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-1 rounded-full overflow-hidden"
                      style={{ background: "rgba(255,255,255,0.06)" }}>
                      <div className="h-full rounded-full"
                        style={{
                          width: `${Math.min(100, Math.max(0, t.progress * 100))}%`,
                          background: "linear-gradient(90deg,#6366f1,#22d3ee)",
                        }} />
                    </div>
                    <span className="text-[10px] tabular-nums shrink-0" style={{ color: "var(--muted)" }}>
                      {Math.round(Math.min(1, Math.max(0, t.progress)) * 100)}%
                    </span>
                  </div>
                  {t.current_stage && (
                    <p className="text-[10px] mt-1 truncate" style={{ color: "var(--muted)" }}>
                      {t.current_stage}
                    </p>
                  )}
                </div>
              )}

              {/* 执行者 */}
              <div className="mt-3 flex items-center gap-2 min-h-6">
                <span className="text-[10px] shrink-0" style={{ color: "var(--muted)" }}>👤</span>
                {executor ? (
                  <>
                    <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                      style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[(i + 2) % AVATAR_GRADS.length]})` }}>
                      {executor.slice(0, 1)}
                    </div>
                    <span className="text-xs truncate">{executor}</span>
                  </>
                ) : (
                  <span className="text-xs" style={{ color: "var(--muted)" }}>未分配</span>
                )}
              </div>

              {/* 操作按钮 */}
              <div className="mt-auto pt-3 mt-3 border-t flex gap-1.5 opacity-0 group-hover:opacity-100 transition-all duration-200"
                style={{ borderColor: "var(--border)" }}
                onClick={(e) => e.stopPropagation()}>
                  {t.status === "PAUSED" ? (
                    <button onClick={() => control(t.id, "resume")}
                      className="flex-1 text-[11px] py-1.5 rounded-lg hover:bg-emerald-500/12 text-emerald-300 transition-colors"
                      style={{ border: "1px solid rgba(16,185,129,0.3)" }}>▶ 恢复</button>
                  ) : (
                    <button onClick={() => canPause && control(t.id, "pause")} disabled={!canPause}
                      title="暂停将在当前步骤完成后生效"
                      className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                        canPause ? "hover:bg-amber-500/12 text-amber-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                      style={{ border: "1px solid var(--border)" }}>⏸ 暂停</button>
                  )}
                  <button onClick={() => canStop && control(t.id, "cancel")} disabled={!canStop}
                    className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                      canStop ? "hover:bg-orange-500/12 text-orange-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                    style={{ border: "1px solid var(--border)" }}>⏹ 停止</button>
                  {t.status === "COMPLETED" && (
                    <button onClick={() => saveAsTemplate(t)} disabled={isBusy}
                      title="把这次成功的任务存为可复用模板（测试模式、默认暂停）"
                      className="flex-1 text-[11px] py-1.5 rounded-lg text-indigo-300 hover:bg-indigo-500/12 transition-colors disabled:opacity-40"
                      style={{ border: "1px solid rgba(99,102,241,0.35)" }}>
                      {isBusy ? "保存中…" : "📋 存为模板"}
                    </button>
                  )}
                  <button onClick={() => requestDelete(t)}
                    className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                    style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑 删除</button>
              </div>
            </div>
          );
        })}

        {loadError && (
          <div className="card card-static text-center py-10 col-span-full space-y-3">
            <p className="text-sm" style={{ color: "#fca5a5" }}>❌ 任务列表加载失败：{loadError}</p>
            <button className="btn-primary !py-1.5 !px-4 text-xs" onClick={() => refresh()}>重试</button>
          </div>
        )}

        {!loadError && !filtered.length && (
          <div className="card card-static text-center py-16 col-span-full text-sm" style={{ color: "var(--muted)" }}>
            {search || dimValue !== "全部" ? "没有符合筛选条件的任务" : "暂无任务"}
          </div>
        )}
      </div>
      </>)}

      {/* C1 收件箱：按"我该处理什么"分桶，计数常驻 */}
      {view === "inbox" && (
        <div>
          <div className="card card-static mb-4 flex items-center gap-1.5 flex-wrap !py-3">
            {INBOX_BUCKETS.map((b) => {
              const on = bucket === b.key;
              const n = inboxData.counts[b.key] ?? 0;
              return (
                <button key={b.key} type="button" data-inbox-bucket={b.key}
                  onClick={() => setBucket(b.key)}
                  className={`px-3 py-1 rounded-full text-xs transition-all border ${
                    on ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                       : "hover:bg-white/[0.04]"}`}
                  style={on ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                  {b.label}
                  <span className={`ml-1.5 tabular-nums ${n ? "" : "opacity-50"}`}>{n}</span>
                </button>
              );
            })}
            <button type="button" onClick={() => setInboxRetry((n) => n + 1)}
              className="ml-auto text-[11px] px-2.5 py-1 rounded-lg hover:bg-white/[0.06] transition-colors"
              style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
              ↻ 刷新
            </button>
          </div>

          {inboxError && (
            <div className="card card-static text-center py-10 space-y-3">
              <p className="text-sm" style={{ color: "#fca5a5" }}>❌ {inboxError}</p>
              <button className="btn-primary !py-1.5 !px-4 text-xs"
                onClick={() => setInboxRetry((n) => n + 1)}>重试</button>
            </div>
          )}

          {!inboxError && inboxLoading && !inbox && (
            <div className="card card-static text-center py-12 text-sm" style={{ color: "var(--muted)" }}>
              收件箱加载中…
            </div>
          )}

          {!inboxError && inbox && (
            <div className="space-y-2">
              {inboxItems.map((item) => {
                const st = STATUS_STYLE[item.status] || STATUS_STYLE.TODO;
                return (
                  <div key={item.id} data-inbox-item={item.id}
                    role="button" tabIndex={0}
                    onClick={() => router.push(`/tasks/${item.task_id || item.id}`)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        router.push(`/tasks/${item.task_id || item.id}`);
                      }
                    }}
                    className="card cursor-pointer flex items-center gap-3 flex-wrap !py-3">
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold text-sm truncate">{item.title}</p>
                      <div className="mt-1 flex items-center gap-2 flex-wrap text-[11px]"
                        style={{ color: "var(--muted)" }}>
                        <span className="inline-flex items-center gap-1">
                          <span className={`w-1.5 h-1.5 rounded-full ${st.dot}`} />
                          <span className={st.cls}>{st.label}</span>
                        </span>
                        <span>👤 {item.agent_name || "未分配"}</span>
                        <span>· 更新于 {timeAgo(item.updated_at) || "—"}</span>
                      </div>
                    </div>
                    {bucket === "awaiting_acceptance" && item.artifact_id && (
                      <button type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          router.push(`/artifacts?open=${item.artifact_id}`);
                        }}
                        className="text-[11px] px-3 py-1.5 rounded-lg font-medium text-emerald-200 hover:bg-emerald-500/15 transition-colors shrink-0"
                        style={{ border: "1px solid rgba(16,185,129,0.4)" }}>
                        去验收 →
                      </button>
                    )}
                  </div>
                );
              })}
              {!inboxItems.length && (
                <div className="card card-static text-center py-12 text-sm" style={{ color: "var(--muted)" }}>
                  {INBOX_BUCKETS.find((b) => b.key === bucket)?.empty}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 新建任务弹窗（仿项目中心新建项目） */}
      {mounted && showCreate && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setShowCreate(false)}>
          <div className="w-full max-w-lg rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 pb-3 border-b" style={{ borderColor: "var(--border)" }}>
              <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg"
                style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>📋</span>
              <h3 className="font-bold text-base flex-1">新建任务</h3>
              <button onClick={() => setShowCreate(false)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-[2fr_auto] gap-2.5">
              <input placeholder="任务标题 *" value={ntForm.title}
                onChange={(e) => setNtForm({ ...ntForm, title: e.target.value })} className="input" />
              <select value={ntForm.priority}
                onChange={(e) => setNtForm({ ...ntForm, priority: e.target.value })} className="input w-auto sm:w-36">
                {Object.entries(PRIORITY_META).map(([k, m]) => (
                  <option key={k} value={k}>优先级：{m.label}</option>
                ))}
              </select>
            </div>
            <textarea rows={2} placeholder="任务详情（可留空）" value={ntForm.description}
              onChange={(e) => setNtForm({ ...ntForm, description: e.target.value })} className="input resize-none" />
            <AttachmentPicker value={ntAttach} onChange={setNtAttach} label="添加任务附件" />

            {/* 创建后立即开工：后端按 worker 队列启动，队满则先排队、稍后自动启动 */}
            <label className="flex items-center gap-2 text-xs cursor-pointer select-none py-0.5"
              style={{ color: "var(--muted)" }}>
              <input type="checkbox" checked={ntAutoStart}
                onChange={(e) => setNtAutoStart(e.target.checked)}
                className="accent-indigo-500 w-3.5 h-3.5" />
              <span>创建后立即开工</span>
              <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                {ntAutoStart ? "· 自动进入执行队列，队满时排队" : "· 先建为待办，稍后手动启动"}
              </span>
            </label>

            {/* 部门化选人 */}
            <div>
              <p className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: "var(--muted)" }}>
                👥 选择执行成员（按部门 · 已选 {picked.size} 人）
                {picked.size > 0 && (
                  <button onClick={() => setPicked(new Set())}
                    className="text-red-300 hover:bg-red-500/10 px-2 py-0.5 rounded-md text-[11px]">清空</button>
                )}
              </p>
              <div className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
                {/* 全员：一键全选 */}
                <div className="rounded-xl overflow-hidden shrink-0"
                  style={{ background: isAllPicked ? "rgba(245,158,11,0.12)" : "rgba(255,255,255,0.03)", border: "1px solid rgba(245,158,11,0.35)" }}>
                  <button type="button" onClick={toggleAll}
                    className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-white/[0.04] transition-colors">
                    <span className={`w-4 h-4 rounded flex items-center justify-center text-[10px] shrink-0 transition-colors ${isAllPicked ? "bg-amber-500" : "border border-white/30"}`}>
                      {isAllPicked && "✓"}
                    </span>
                    <span className="w-8 h-8 rounded-lg flex items-center justify-center text-base shrink-0"
                      style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>🌍</span>
                    <span className="flex-1 min-w-0">
                      <span className="text-sm font-semibold block">全员执行</span>
                      <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                        {isAllPicked ? `全员参与 · ${allAgentIds.length} 人` : `一键选择所有人员（${allAgentIds.length} 人）`}
                      </span>
                    </span>
                    {isAllPicked && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 shrink-0">全体参与</span>
                    )}
                  </button>
                </div>
                {/* 按部门 / 跨部门 / 单人 */}
                {deptGroups.map((g) => {
                  const all = isDeptAllPicked(g);
                  const some = !all && g.agents.some((a) => picked.has(a.id));
                  const open = deptOpen.has(g.dept_id);
                  const grad = AVATAR_GRADS[Math.abs(
                    g.dept_name.split("").reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_GRADS.length];
                  return (
                    <div key={g.dept_id} className="rounded-xl overflow-hidden"
                      style={{ background: "var(--surface-2)", border: all ? "1px solid rgba(99,102,241,0.45)" : "1px solid var(--border)" }}>
                      <div className="flex items-center gap-2 px-3 py-2">
                        <button type="button" onClick={() => toggleDept(g)}
                          className={`w-4 h-4 rounded border flex items-center justify-center text-[9px] shrink-0 transition-all ${
                            all ? "bg-indigo-500 border-indigo-500 text-white"
                              : some ? "border-indigo-400 text-indigo-300" : "border-white/25"}`}
                          title={all ? "取消整部门" : "选择整部门"}>
                          {all ? "✓" : some ? "−" : ""}
                        </button>
                        <span className="w-6 h-6 rounded-lg flex items-center justify-center text-xs shrink-0"
                          style={{ background: `linear-gradient(135deg,${grad})` }}>🏢</span>
                        <button type="button" onClick={() => setDeptOpen((prev) => {
                          const n = new Set(prev);
                          if (n.has(g.dept_id)) n.delete(g.dept_id); else n.add(g.dept_id);
                          return n;
                        })}
                          className="flex-1 text-left text-sm font-medium flex items-center gap-2">
                          {g.dept_name}
                          <span className="text-[10px]" style={{ color: "var(--muted)" }}>{g.agents.length} 人</span>
                          <span className={`text-indigo-300 text-[10px] transition-transform inline-block ${open ? "rotate-90" : ""}`}>▸</span>
                        </button>
                        <span className="text-[10px]" style={{ color: all ? "#818cf8" : "var(--muted)" }}>
                          {all ? "全员参会" : some ? "部分参会" : ""}
                        </span>
                      </div>
                      {open && (
                        <div className="px-3 pb-2 flex flex-wrap gap-1.5 animate-fade-up">
                          {g.agents.map((a) => {
                            const on = picked.has(a.id);
                            return (
                              <button key={a.id} type="button" onClick={() => toggleAgent(a.id)}
                                className={`px-2.5 py-1 rounded-lg text-[11px] border transition-all ${
                                  on ? "bg-indigo-500/20 border-indigo-400 text-indigo-200"
                                     : "hover:bg-white/[0.06]"}`}
                                style={on ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                                {on ? "✓ " : ""}{a.name}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
                {!deptGroups.length && (
                  <p className="text-xs py-3 text-center" style={{ color: "var(--muted)" }}>暂无部门数据</p>
                )}
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 pt-2">
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={createTask} disabled={!ntForm.title.trim() || !picked.size || creating}
                className="btn-primary disabled:opacity-40">
                {creating ? "分发中…" : `🚀 创建并分发给 ${picked.size} 名成员`}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 删除任务确认弹窗（Portal，与项目中心一致） */}
      {mounted && confirmTarget && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setConfirmTarget(null)}>
          <div className="w-full max-w-sm rounded-2xl p-5 space-y-4 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.45)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3">
              <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                style={{ background: "linear-gradient(135deg,#ef4444,#f59e0b)" }}>⚠️</span>
              <p className="text-sm leading-relaxed whitespace-pre-wrap flex-1">
                彻底删除任务「{confirmTarget.t.title}」？{"\n"}所有记忆、成果、执行记录将全部清除，不可恢复！
              </p>
            </div>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmTarget(null)}
                className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={executeDelete}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90 transition-all">
                确认彻底删除
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 批量删除弹窗 */}
      {showBatchDelete && typeof document !== "undefined" &&
        createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => { setShowBatchDelete(false); setBatchSel(new Set()); }}>
          <div className="w-full max-w-2xl rounded-2xl overflow-hidden animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(248,113,113,0.25)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="px-5 pt-5 pb-3 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <div>
                <h3 className="font-bold text-sm">🗑 批量删除任务</h3>
                <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  已选 {batchSel.size} 个（当前筛选范围内共 {filtered.length} 个）· 关联记忆/成果将全部清除
                </p>
              </div>
              <button onClick={() => { setShowBatchDelete(false); setBatchSel(new Set()); }}
                className="btn-ghost !px-2 !py-1 text-xs">✕</button>
            </div>
            <div className="px-5 pt-3 pb-1 flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <label className="flex items-center gap-2 text-xs cursor-pointer select-none" style={{ color: "var(--muted)" }}>
                <input type="checkbox"
                  checked={filtered.length > 0 && batchSel.size === filtered.length}
                  onChange={() => setBatchSel(new Set(filtered.map((x: any) => x.id)))}
                  className="accent-indigo-500 w-3.5 h-3.5" />
                全选
              </label>
              {batchSel.size > 0 && (
                <button onClick={() => setBatchSel(new Set())} className="text-[11px] hover:text-[var(--foreground)] transition-colors">清空</button>
              )}
            </div>
            <div className="max-h-[50vh] overflow-y-auto p-4 grid grid-cols-2 gap-2">
              {filtered.map((t, i) => {
                const on = batchSel.has(t.id);
                return (
                  <button key={t.id} type="button"
                    onClick={() => toggleBatchSel(t.id)}
                    className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${
                      on ? "border-red-500/60 bg-red-500/10" : "hover:bg-white/[0.04]"}`}
                    style={on ? {} : { borderColor: "var(--border)" }}>
                    <div className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs shrink-0"
                      style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[i % AVATAR_GRADS.length]})` }}>◉</div>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium truncate">{t.title}</div>
                      <div className="text-[10px]" style={{ color: "var(--muted)" }}>
                        {STATUS_STYLE[t.status]?.label || t.status}
                      </div>
                    </div>
                    {on && <span className="text-red-400 text-xs shrink-0">✓</span>}
                  </button>
                );
              })}
            </div>
            <div className="px-5 py-4 border-t flex items-center gap-3" style={{ borderColor: "var(--border)" }}>
              <button onClick={batchDeleteTasks} disabled={!batchSel.size || batchDeleting}
                className="disabled:opacity-40 px-5 py-2 rounded-lg text-sm font-medium text-white transition-all"
                style={{
                  background: "linear-gradient(135deg,#ef4444,#f97316)",
                  boxShadow: "0 2px 12px -2px rgba(239,68,68,0.5)",
                }}>
                {batchDeleting ? "删除中…" : `确认删除 ${batchSel.size} 个任务`}
              </button>
              <button onClick={() => setBatchSel(new Set())} className="btn-ghost text-xs">清空选择</button>
              <p className="text-[11px] ml-auto" style={{ color: "var(--muted)" }}>⚠️ 删除不可恢复</p>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
