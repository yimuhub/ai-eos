"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { createPortal } from "react-dom";
import { api, setActiveCompanyId } from "@/lib/api";
import { useVisibilityInterval } from "@/lib/useResource";
import TeamGenModal from "@/components/TeamGenModal";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import OnboardingWizard from "@/components/OnboardingWizard";
import { notify } from "@/components/Toast";
import { TASK_STATUS_ZH, EVENT_TYPE_ZH, zh } from "@/lib/i18n";
import { confirmAction } from "@/components/ConfirmDialog";

/* ============================================================
   指挥中心 v2：KPI 概览 · 新建任务弹窗（同任务中心）·
   运行/排队任务管理（原任务调度中心） · 全局中断
   ============================================================ */

type Company = { id: string; name: string };
type Dash = {
  agents: Record<string, number>;
  tasks: Record<string, any>;
  recent_assets: { id: string; name: string; type: string }[];
  recent_events: { event_type: string; payload: any; created_at: string }[];
};
type RTask = { id: string; title: string; status: string; priority: string };
type Briefing = {
  greeting: string;
  yesterday: { completed_tasks: { title: string }[]; automation_ok: number; automation_failed: number };
  today: { active_tasks: { title: string; status: string }[]; todo_count: number;
           meetings: { title: string; status: string }[]; automation_runs_today: number };
  needs_you: { count: number; items: { kind: string; text: string; goto: string }[] };
  cost_today: { cost: number; tokens: number };
  /* V5.5 A2：预算软预警（≥80% 黄色提示，到顶自动暂停） */
  budget_alert?: { pct: number; paused: boolean; message: string } | null;
};

const TASK_BADGE: Record<string, { cls: string; dot: string }> = {
  RUNNING: { cls: "text-cyan-300", dot: "bg-cyan-400" },
  COMPLETED: { cls: "text-emerald-300", dot: "bg-emerald-400" },
  FAILED: { cls: "text-red-300", dot: "bg-red-400" },
  TODO: { cls: "text-[var(--muted)]", dot: "bg-white/30" },
};

const PRIORITY_META: Record<string, { label: string; cls: string; dot: string }> = {
  LOW: { label: "低", cls: "text-[var(--muted)]", dot: "bg-white/30" },
  NORMAL: { label: "普通", cls: "text-sky-300", dot: "bg-sky-400" },
  HIGH: { label: "高", cls: "text-amber-300", dot: "bg-amber-400" },
  URGENT: { label: "紧急", cls: "text-red-300", dot: "bg-red-400" },
};
const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e"];

export default function DashboardPage() {
  const [mounted, setMounted] = useState(false);
  const [company, setCompany] = useState<Company | null>(null);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [dash, setDash] = useState<Dash | null>(null);
  const [msg, setMsg] = useState("");

  // 新建任务弹窗（同任务中心）
  const [showCreate, setShowCreate] = useState(false);
  const [ntForm, setNtForm] = useState({ title: "", description: "", priority: "NORMAL" });
  const [deptGroups, setDeptGroups] = useState<{ dept_id: string; dept_name: string; agents: { id: string; name: string }[] }[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [deptOpen, setDeptOpen] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [ntAttach, setNtAttach] = useState<Attachment[]>([]);

  // 运行中/排队任务（原任务调度）
  const [runningTasks, setRunningTasks] = useState<RTask[]>([]);
  const [taskSel, setTaskSel] = useState<Set<string>>(new Set());
  const [bulkPriority, setBulkPriority] = useState("HIGH");

  // 全局中断
  const [confirmInterrupt, setConfirmInterrupt] = useState(false);
  const [interrupting, setInterrupting] = useState(false);

  // 创建新团队向导（共享组件）
  const [showTeamGen, setShowTeamGen] = useState(false);

  // 冷启动向导：没有公司时不再白屏，而是给出可执行的三步引导
  const [onboarding, setOnboarding] = useState(false);
  const [bootError, setBootError] = useState("");

  // V5.5 P0：CEO 晨间简报（昨日完成 / 今日计划 / 需要你决策）
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  // V5.5 A1：每日简报推送状态 + 系统通知
  const [pushEnabled, setPushEnabled] = useState<boolean | null>(null);
  const lastBriefIdRef = useRef<string>("");

  useEffect(() => { setMounted(true); }, []);

  /* V5.5 A1：查询是否已开启每日简报推送 */
  useEffect(() => {
    const cid = localStorage.getItem("aieos_active_cid");
    if (!cid) return;
    api.get<{ action_type?: string; name?: string }[]>(
      `/schedules?company_id=${cid}`).then((r) => {
      setPushEnabled(r.some((s) => s.action_type === "daily_briefing"));
    }).catch(() => { setPushEnabled(false); });
  }, []);

  const enablePush = async () => {
    const cid = localStorage.getItem("aieos_active_cid");
    if (!cid) return;
    try {
      await api.post(`/briefing/push-schedule?company_id=${cid}&time_str=09:00`);
      setPushEnabled(true);
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        void Notification.requestPermission();
      }
    } catch { /* 推送失败不影响主流程 */ }
  };

  const load = useCallback(async () => {
    // 原实现在这里没有 try/catch，且"没有公司"时直接 return，
    // 结果是用户看到一片空白页且完全不知道发生了什么。
    // 现在：明确区分"加载失败（可重试）"与"还没有公司（走引导向导）"。
    let companies: Company[] = [];
    try {
      companies = await api.get<Company[]>("/companies");
    } catch (e) {
      setBootError(e instanceof Error ? e.message : "加载失败");
      return;
    }
    setBootError("");
    if (!companies.length) {
      setOnboarding(true);
      return;
    }
    setOnboarding(false);
    setCompanies(companies);
    // 拦截层已把激活公司排第一；若 localStorage 为空则初始化为第一项
    const cur = companies[0];
    if (!localStorage.getItem("aieos_active_cid")) {
      localStorage.setItem("aieos_active_cid", cur.id);
    }
    setCompany(cur);
    const cid = cur.id;
    try {
      setDash(await api.get(`/dashboard/${cid}`));
    } catch (e) {
      // 仪表盘失败不应让整页空白，也不该静默
      notify("error", e instanceof Error ? e.message : "加载仪表盘失败");
    }
    try {
      const ov = await api.get(`/growth-center/departments-overview?company_id=${cid}`);
      setDeptGroups((ov.departments || []).map((dep: any) => ({
        dept_id: dep.dept_id, dept_name: dep.dept_name,
        agents: (dep.agents || []).map((a: any) => ({ id: a.id, name: a.name })),
      })));
    } catch {
      // 部门列表缺失只影响"按部门选人"，不阻塞主流程，保持为空
    }
    try {
      const tasks = await api.get(`/tasks?company_id=${cid}`);
      setRunningTasks((Array.isArray(tasks) ? tasks : []).filter((t: any) =>
        ["TODO", "PLANNING", "RUNNING", "WAITING"].includes(t.status)));
    } catch {
      setRunningTasks([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /* V5.5 P0：CEO 晨间简报低频刷新（30s），不跟 5s 实时循环抢连接 */
  const loadBriefing = useCallback(async () => {
    const cid = localStorage.getItem("aieos_active_cid");
    if (!cid) return;
    try { setBriefing(await api.get<Briefing>(`/briefing?company_id=${cid}`)); }
    catch { /* 简报失败不打扰指挥中心主流程 */ }
    /* V5.5 A1：有新简报推送 + 浏览器通知已授权 → 弹系统通知 */
    try {
      const evs = await api.get<{ id: string; payload?: Record<string, unknown> }[]>(
        `/events?event_type=BRIEFING_PUSH&limit=1`);
      const ev = evs[0];
      if (ev && ev.id !== lastBriefIdRef.current) {
        lastBriefIdRef.current = ev.id;
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          const b = (ev.payload?.briefing ?? {}) as Partial<Briefing>;
          const n = b.needs_you?.count ?? 0;
          new Notification("🌅 CEO 每日简报", {
            body: `${b.greeting || ""} · 需要你决策 ${n} 件${n > 0 ? "：" : ""} ` +
              ((b.needs_you?.items || []).slice(0, 2).map((i) => i.text).join("；")),
          });
        }
      }
    } catch { /* 事件拉取失败不打扰 */ }
  }, []);
  useEffect(() => {
    loadBriefing();
    const t = window.setInterval(loadBriefing, 30000);
    return () => window.clearInterval(t);
  }, [loadBriefing]);

  /* 指挥中心需要"实时"：任务正在跑，5 秒一次的新鲜度是合理的。
     但**页面在后台时不该继续打后端**——用户看不见的刷新是纯粹的成本，
     还可能与真正的前台请求抢连接。切回来自动补一次，避免第一眼是旧数据。 */
  useVisibilityInterval(load, 5000);

  useEffect(() => {
    const onSwitch = () => { load(); loadBriefing(); };
    window.addEventListener("aieos-company-changed", onSwitch);
    return () => window.removeEventListener("aieos-company-changed", onSwitch);
  }, [load, loadBriefing]);

  /* ---------- 部门化选人 ---------- */
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

  /* ---------- 新建并分发任务（同任务中心） ---------- */
  async function createTask() {
    if (!ntForm.title.trim() || !picked.size) return;
    setCreating(true);
    try {
      const attachNote = ntAttach.length
        ? `\n\n📎 附件：${ntAttach.map((a) => a.name).join("、")}`
        : "";
      // 改走统一 api 层：自动注入鉴权、超时、错误规范化（原先裸 fetch + 手工拼 token）
      const data = await api.post<{ created_count?: number }>(
        "/task-dispatch/dispatch",
        {
          company_id: company?.id,
          title: ntForm.title.trim(),
          description: (ntForm.description || null)
            ? ntForm.description + attachNote : (attachNote ? attachNote.trim() : null),
          priority: ntForm.priority,
          target_type: "multi_agent_list",
          agent_ids: Array.from(picked),
        },
      );
      notify("success", `已创建任务「${ntForm.title.trim()}」· 分发给 ${data?.created_count ?? picked.size} 名成员`);
      setShowCreate(false);
      setNtForm({ title: "", description: "", priority: "NORMAL" });
      setPicked(new Set()); setNtAttach([]);
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    }
    setCreating(false);
  }

  /* ---------- 运行/排队任务操作 ---------- */
  async function taskOp(tid: string, op: "pause" | "resume" | "stop") {
    const ep = { pause: "pause", resume: "resume", stop: "cancel" }[op];
    const zhLabel = { pause: "已暂停（进度保留）", resume: "已恢复执行", stop: "已停止（成果保留）" };
    try {
      // 关键修复：原实现 `await fetch(...)` 后**不看响应**就提示"✅ 已暂停"，
      // 即使后端返回 403/500 也一样报喜——这是"系统会说谎"最典型的形态。
      await api.post(`/tasks/${tid}/${ep}`,
        op === "stop" ? { reason: "manual stop" } : undefined);
      notify("success", zhLabel[op]);
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    }
  }

  async function changePriority() {
    if (!taskSel.size) return;
    const qs = new URLSearchParams({ company_id: company?.id || "" });
    Array.from(taskSel).forEach((id) => qs.append("task_ids", id));
    try {
      const data = await api.patch<{ updated?: number }>(
        `/task-dispatch/running/priority?${qs.toString()}`,
        { priority: bulkPriority },
      );
      notify("success", `已调整 ${data?.updated ?? taskSel.size} 个任务优先级为「${PRIORITY_META[bulkPriority]?.label}」`);
      setTaskSel(new Set());
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    }
  }

  async function deleteSelected() {
    if (!taskSel.size) return;
    if (!(await confirmAction({
      title: `确认彻底删除这 ${taskSel.size} 个任务？`,
      description: "批量删除会一次性清空多个任务的执行痕迹，请先确认选中的范围。",
      bullets: [
        "这些任务的记忆、成果、执行记录将全部清除",
        "若任务仍在执行，会被立即中止",
        "此操作不可恢复",
      ],
      confirmPhrase: "删除",
      confirmText: "删除",
    }))) return;
    try {
      await api.post("/task-dispatch/delete", { task_ids: Array.from(taskSel) });
      notify("success", `已彻底删除 ${taskSel.size} 个任务`);
      setTaskSel(new Set());
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    }
  }

  /* ---------- 全局中断 ---------- */
  async function globalInterrupt() {
    setInterrupting(true);
    try {
      const data = await api.post<{ interrupted_count?: number }>(
        `/task-dispatch/global-interrupt?company_id=${company?.id}&reason=manual`);
      notify("success", `已中断 ${data?.interrupted_count ?? 0} 个运行中任务（记录保留）`);
      setConfirmInterrupt(false);
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    } finally {
      setInterrupting(false);
    }
  }

  /* ---------- 单任务彻底删除（原为裸 fetch 且忽略失败） ---------- */
  async function deleteOne(tid: string, title: string) {
    if (!(await confirmAction({
      title: `确认彻底删除任务「${title.slice(0, 25)}」？`,
      description: "该任务的历史将不再可查，包括它产生的结论与经验。",
      bullets: ["记忆、成果、执行记录将全部清除", "此操作不可恢复"],
      confirmText: "删除",
    }))) return;
    try {
      await api.post("/task-dispatch/delete", { task_ids: [tid] });
      notify("success", "已彻底删除该任务");
      await load();
    } catch (e: any) {
      notify("error", e?.message || String(e));
    }
  }

  const prioBadge = (p: string) => {
    const st = PRIORITY_META[p] || PRIORITY_META.NORMAL;
    return (
      <span className="inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full font-medium shrink-0"
        style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}>
        <span className={`w-1.5 h-1.5 rounded-full ${st.dot}`} />
        <span className={st.cls}>{st.label}</span>
      </span>
    );
  };

  const agentCards = [
    { k: "total", label: "在线 Agent", accent: "#818cf8" },
    { k: "running", label: "工作中", accent: "#22d3ee" },
    { k: "idle", label: "空闲", accent: "#94a3b8" },
    { k: "paused", label: "已暂停", accent: "#fbbf24" },
    { k: "error", label: "异常", accent: "#f87171" },
  ];

  return (
    <div className="p-6 space-y-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* ===== 冷启动：没有公司时给出可执行的引导，而不是空白页 ===== */}
      {onboarding && (
        <OnboardingWizard
          onDone={async () => {
            setOnboarding(false);
            await load();
          }}
        />
      )}

      {/* ===== 加载失败：说清原因并允许重试 ===== */}
      {!onboarding && bootError && (
        <div className="card card-static max-w-lg mx-auto text-center space-y-3 mt-10">
          <div className="text-2xl">🔌</div>
          <h2 className="font-medium">加载公司信息失败</h2>
          <p className="text-sm" style={{ color: "var(--muted)" }}>{bootError}</p>
          <button onClick={load} className="btn-primary">重试</button>
        </div>
      )}

      {!onboarding && !bootError && (<>
      {/* 页头 */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">指挥中心</h1>
          <p className="text-sm mt-1 flex items-center gap-1.5" style={{ color: "var(--muted)" }}>
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 pulse-dot" />
            {company?.name || "加载中..."} · 实时运行中
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select value={company?.id || ""}
            onChange={(e) => {
              const v = e.target.value;
              if (v && v !== company?.id) {
                setActiveCompanyId(v);
                setCompany(companies.find((c) => c.id === v) || company);
                load();
              }
            }}
            className="input w-auto !py-2 text-xs font-medium"
            title="切换公司（全站数据随之切换）">
            {companies.map((c) => (
              <option key={c.id} value={c.id}>🏢 {c.name}</option>
            ))}
          </select>
          <button onClick={() => setShowTeamGen(true)}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white transition-all"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)",
                     boxShadow: "0 2px 12px -2px rgba(139,92,246,0.5)" }}>
            ✨ 创建新团队
          </button>
          <button onClick={() => setConfirmInterrupt(true)}
            disabled={!runningTasks.length}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white transition-all disabled:opacity-40"
            style={{
              background: "linear-gradient(135deg,#ef4444,#f97316)",
              boxShadow: "0 2px 12px -2px rgba(239,68,68,0.5)",
            }}>
            🛑 全局中断
          </button>
          <button onClick={() => setShowCreate(true)} className="btn-primary">＋ 下达任务</button>
        </div>
      </div>

      {msg && (
        <div className="px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg}
        </div>
      )}

      {/* ===== V5.5 P0：CEO 晨间简报（一人公司老板视角）===== */}
      {briefing && (
        <div className="card card-static relative overflow-hidden">
          <div className="absolute top-0 left-0 w-full h-[2px]"
            style={{ background: "linear-gradient(90deg, #f59e0b, #ef4444, transparent)" }} />
          <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
            <div>
              <h2 className="font-semibold text-sm">🌅 {briefing.greeting || "CEO 晨间简报"}</h2>
              <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                昨日完成 · 今日计划 · 需要你决策
              </p>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="px-2 py-1 rounded-full font-medium"
                style={{ background: "rgba(245,158,11,0.12)", color: "#fbbf24",
                        border: "1px solid rgba(245,158,11,0.3)" }}>
                今日已用 ¥{briefing.cost_today?.cost ?? 0} · {briefing.cost_today?.tokens ?? 0} token
              </span>
              {briefing.budget_alert && (
                <span className="px-2 py-1 rounded-full font-medium animate-pulse"
                  style={{ background: "rgba(239,68,68,0.12)", color: "#f87171",
                          border: "1px solid rgba(239,68,68,0.3)" }}>
                  🛡 {briefing.budget_alert.paused ? "预算已暂停" : `预算 ${briefing.budget_alert.pct}%`}
                </span>
              )}
              {briefing.needs_you?.count > 0 && (
                <span className="px-2 py-1 rounded-full font-medium"
                  style={{ background: "rgba(239,68,68,0.12)", color: "#f87171",
                          border: "1px solid rgba(239,68,68,0.3)" }}>
                  ⚠ {briefing.needs_you.count} 件事需要你
                </span>
              )}
              {/* V5.5 A1：每日简报推送开关 */}
              <button onClick={enablePush} disabled={pushEnabled === true}
                className="px-2 py-1 rounded-full font-medium hover:opacity-80 transition"
                style={pushEnabled
                  ? { background: "rgba(16,185,129,0.12)", color: "#34d399",
                      border: "1px solid rgba(16,185,129,0.3)", cursor: "default" }
                  : { background: "rgba(34,211,238,0.1)", color: "#22d3ee",
                      border: "1px solid rgba(34,211,238,0.3)" }}>
                {pushEnabled ? "✓ 每日 09:00 自动推送简报" : "🔔 开启每日 09:00 简报推送"}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
            <div className="rounded-lg p-3"
              style={{ background: "rgba(16,185,129,0.06)", border: "1px solid rgba(16,185,129,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#34d399" }}>
                ✅ 昨日完成
              </div>
              <ul className="space-y-1 text-xs">
                {(briefing.yesterday?.completed_tasks || []).slice(0, 3).map((t, i) => (
                  <li key={i} className="truncate" style={{ color: "var(--muted)" }}>· {t.title}</li>
                ))}
                {(briefing.yesterday?.completed_tasks || []).length === 0 && (
                  <li style={{ color: "var(--muted)" }}>暂无完成任务</li>
                )}
                <li style={{ color: "var(--muted)" }}>
                  自动化：{briefing.yesterday?.automation_ok ?? 0} 成功
                  {briefing.yesterday?.automation_failed ? ` · ${briefing.yesterday.automation_failed} 失败` : ""}
                </li>
              </ul>
            </div>
            <div className="rounded-lg p-3"
              style={{ background: "rgba(34,211,238,0.06)", border: "1px solid rgba(34,211,238,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#22d3ee" }}>
                📋 今日计划
              </div>
              <ul className="space-y-1 text-xs">
                {(briefing.today?.active_tasks || []).slice(0, 2).map((t, i) => (
                  <li key={i} className="truncate" style={{ color: "var(--foreground)" }}>
                    · {t.title} <span style={{ color: "var(--muted)" }}>({t.status})</span>
                  </li>
                ))}
                <li style={{ color: "var(--muted)" }}>
                  待办 {briefing.today?.todo_count ?? 0} · 会议
                  {(briefing.today?.meetings || []).slice(0, 1).map((m, i) => (
                    <span key={i} className="ml-1">{m.title}</span>
                  ))}
                </li>
                <li style={{ color: "var(--muted)" }}>今日自动化已跑 {briefing.today?.automation_runs_today ?? 0} 次</li>
              </ul>
            </div>
            <div className="rounded-lg p-3"
              style={{ background: "rgba(239,68,68,0.05)", border: "1px solid rgba(239,68,68,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#f87171" }}>
                ⚠ 需要你决策（{briefing.needs_you?.count ?? 0}）
              </div>
              <ul className="space-y-1">
                {(briefing.needs_you?.items || []).slice(0, 3).map((it, i) => (
                  <li key={i}>
                    <Link href={it.goto}
                      className="text-xs hover:underline block truncate"
                      style={{ color: "var(--foreground)" }}>
                      <span style={{ color: "#fbbf24" }}>[{it.kind}]</span> {it.text}
                    </Link>
                  </li>
                ))}
                {(briefing.needs_you?.count ?? 0) === 0 && (
                  <li className="text-xs" style={{ color: "var(--muted)" }}>
                    一切正常，无需干预 👌
                  </li>
                )}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Agent 状态卡 */}
      <div className="grid grid-cols-5 gap-3">
        {agentCards.map(({ k, label, accent }) => (
          <div key={k} className="card relative overflow-hidden">
            <div className="absolute top-0 left-0 w-full h-[2px]"
              style={{ background: `linear-gradient(90deg, ${accent}, transparent)` }} />
            <div className="flex items-baseline gap-1.5">
              <span className="text-[26px] font-bold tabular-nums" style={{ color: accent }}>
                {dash?.agents?.[k] ?? 0}
              </span>
            </div>
            <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>{label}</div>
          </div>
        ))}
      </div>

      {/* 任务概览 + 进行中任务 */}
      <div className="grid grid-cols-3 gap-6">
        <div className="card card-static">
          <h2 className="text-xs font-semibold tracking-widest uppercase mb-3" style={{ color: "var(--muted)" }}>
            任务概览
          </h2>
          <div className="space-y-3">
            {[["running", "进行中"], ["completed", "已完成"], ["failed", "失败"], ["todo", "待处理"]].map(([k, label]) => {
              const b = TASK_BADGE[k.toUpperCase()];
              return (
                <div key={k} className="flex items-center gap-2.5">
                  <span className={`w-2 h-2 rounded-full ${b.dot}`} />
                  <span className="text-sm flex-1">{label}</span>
                  <span className="text-lg font-bold tabular-nums">{dash?.tasks?.[k] ?? 0}</span>
                </div>
              );
            })}
          </div>
          <div className="mt-4 pt-3 border-t" style={{ borderColor: "var(--border)" }}>
            <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
              {(() => {
                const done = dash?.tasks?.completed ?? 0;
                const total = (dash?.tasks?.running ?? 0) + done + (dash?.tasks?.failed ?? 0) + (dash?.tasks?.todo ?? 0);
                return (
                  <div className="h-full rounded-full transition-all duration-500"
                    style={{ width: total ? `${(done / total) * 100}%` : "0%", background: "linear-gradient(90deg,#6366f1,#22d3ee)" }} />
                );
              })()}
            </div>
            <p className="text-[10px] mt-1.5" style={{ color: "var(--muted)" }}>完成率</p>
          </div>
        </div>

        {/* 进行中任务 */}
        <div className="col-span-2">
          <h2 className="text-xs font-semibold tracking-widest uppercase mb-3" style={{ color: "var(--muted)" }}>
            进行中的任务
          </h2>
          <div className="space-y-2">
            {(dash?.tasks?.active || []).length === 0 && (
              <div className="card card-static text-center py-8 text-xs" style={{ color: "var(--muted)" }}>
                暂无进行中的任务
              </div>
            )}
            {(dash?.tasks?.active || []).map((t: any) => {
              const b = TASK_BADGE[t.status] || TASK_BADGE.TODO;
              return (
                <Link key={t.id} href={`/tasks/${t.id}`} className="card block">
                  <div className="flex justify-between items-center">
                    <span className="text-sm font-medium">{t.title}</span>
                    <span className={`badge ${b.cls}`}
                      style={{ borderColor: "rgba(255,255,255,0.08)", background: "rgba(255,255,255,0.03)" }}>
                      <span className={`w-1 h-1 rounded-full ${b.dot}`} />
                      {zh(TASK_STATUS_ZH, t.status)}
                    </span>
                  </div>
                  <p className="text-xs mt-1.5" style={{ color: "var(--muted)" }}>
                    优先级：{t.priority === "URGENT" ? "🔴 紧急" : t.priority === "HIGH" ? "🟠 高" : t.priority === "LOW" ? "🔵 低" : "⚪ 普通"}
                  </p>
                </Link>
              );
            })}
          </div>
        </div>
      </div>

      {/* ===== 运行中 / 排队任务管理（原任务调度中心） ===== */}
      <div className="card card-static">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h2 className="font-semibold text-sm">
            ⚡ 运行中 / 排队任务（{runningTasks.length}）
          </h2>
          <div className="flex items-center gap-2">
            {taskSel.size > 0 && (
              <>
                <span className="text-xs" style={{ color: "var(--muted)" }}>已选 {taskSel.size}</span>
                <select value={bulkPriority} onChange={(e) => setBulkPriority(e.target.value)}
                  className="input w-auto !py-1.5 text-xs">
                  {Object.entries(PRIORITY_META).map(([k, m]) => (
                    <option key={k} value={k}>{m.label}</option>
                  ))}
                </select>
                <button onClick={changePriority} className="btn-primary !py-1.5 text-xs">
                  调整优先级
                </button>
                <button onClick={deleteSelected}
                  className="text-xs py-1.5 px-3 rounded-lg text-red-300 hover:bg-red-500/15 transition-colors"
                  style={{ border: "1px solid rgba(248,113,113,0.35)" }}>
                  🗑 批量删除
                </button>
                <button onClick={() => setTaskSel(new Set())} className="btn-ghost !py-1.5 text-xs">✕</button>
              </>
            )}
          </div>
        </div>

        {runningTasks.length ? (
          <div className="space-y-1.5 max-h-[420px] overflow-y-auto p-1">
            {runningTasks.map((t) => {
              const on = taskSel.has(t.id);
              const grad = AVATAR_GRADS[Math.abs(
                t.title.split("").reduce((a: number, c: string) => a + c.charCodeAt(0), 0)) % AVATAR_GRADS.length];
              return (
                <div key={t.id}
                  onClick={() => setTaskSel((prev) => {
                    const n = new Set(prev); if (n.has(t.id)) n.delete(t.id); else n.add(t.id); return n;
                  })}
                  className={`group w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-left cursor-pointer transition-all border ${
                    on ? "border-indigo-500/60 bg-indigo-500/10" : "hover:bg-white/[0.03]"}`}
                  style={on ? {} : { borderColor: "transparent" }}>
                  <span className={`shrink-0 w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${
                    on ? "bg-indigo-500 border-indigo-500 text-white" : ""}`}
                    style={!on ? { borderColor: "var(--border)" } : {}}>{on && "✓"}</span>
                  <Link href={`/tasks/${t.id}`} onClick={(e) => e.stopPropagation()}
                    className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs font-bold shrink-0"
                    style={{ background: `linear-gradient(135deg, ${grad})` }}
                    title="打开任务详情">▶</Link>
                  <Link href={`/tasks/${t.id}`} onClick={(e) => e.stopPropagation()}
                    className="text-sm truncate flex-1 hover:text-indigo-300 transition-colors">{t.title}</Link>
                  {prioBadge(t.priority)}
                  <span className="text-[10px] px-2 py-1 rounded-full shrink-0"
                    style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                    {t.status === "RUNNING" ? "执行中" : t.status === "PAUSED" ? "已暂停" :
                     t.status === "TODO" ? "待执行" : t.status === "PLANNING" ? "规划中" : t.status}
                  </span>
                  <span className="flex gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                    onClick={(e) => e.stopPropagation()}>
                    {t.status === "RUNNING" && (
                      <span title="暂停" onClick={() => taskOp(t.id, "pause")}
                        className="w-6 h-6 rounded-md flex items-center justify-center text-xs hover:bg-amber-500/20 cursor-pointer">⏸</span>
                    )}
                    {t.status === "PAUSED" && (
                      <span title="恢复" onClick={() => taskOp(t.id, "resume")}
                        className="w-6 h-6 rounded-md flex items-center justify-center text-xs hover:bg-emerald-500/20 cursor-pointer">▶</span>
                    )}
                    <span title="停止（成果保留）" onClick={() => taskOp(t.id, "stop")}
                      className="w-6 h-6 rounded-md flex items-center justify-center text-xs hover:bg-orange-500/20 cursor-pointer">⏹</span>
                    <span title="彻底删除" onClick={() => deleteOne(t.id, t.title)}
                      className="w-6 h-6 rounded-md flex items-center justify-center text-xs hover:bg-red-500/20 text-red-400 cursor-pointer">🗑</span>
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
            当前没有运行中或排队的任务
          </p>
        )}
      </div>

      {/* 实时动态 + 最近成果 */}
      <div className="grid grid-cols-3 gap-6">
        <div className="col-span-2 card card-static">
          <h2 className="text-xs font-semibold tracking-widest uppercase mb-3 flex items-center gap-2" style={{ color: "var(--muted)" }}>
            实时动态
            <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 pulse-dot" />
          </h2>
          <div className="divide-y max-h-64 overflow-auto" style={{ borderColor: "var(--border)" }}>
            {(dash?.recent_events || []).map((e, i) => (
              <div key={i} className="py-2 text-xs flex justify-between items-center">
                <span className="flex items-center gap-2">
                  <span className="w-1 h-1 rounded-full bg-indigo-400/60" />
                  {zh(EVENT_TYPE_ZH, e.event_type)}
                </span>
                <span className="tabular-nums" style={{ color: "var(--muted)" }}>
                  {new Date(e.created_at).toLocaleTimeString()}
                </span>
              </div>
            ))}
            {!(dash?.recent_events || []).length && (
              <p className="py-6 text-center text-xs" style={{ color: "var(--muted)" }}>暂无动态</p>
            )}
          </div>
        </div>

        {(dash?.recent_assets?.length ?? 0) > 0 && (
          <div className="card card-static">
            <h2 className="text-xs font-semibold tracking-widest uppercase mb-3" style={{ color: "var(--muted)" }}>最近成果</h2>
            <div className="space-y-1.5">
              {(dash?.recent_assets ?? []).map((a) => (
                <div key={a.id} className="flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs"
                  style={{ background: "var(--surface-2)" }}>
                  <span>📄</span>
                  <span className="truncate">{a.name}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ===== 新建任务弹窗（同任务中心） ===== */}
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
              <h3 className="font-bold text-base flex-1">下达任务</h3>
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
                {/* 全员 */}
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
                              : some ? "border-indigo-400 text-indigo-300" : "border-white/25"}`}>
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

      {/* ===== 创建新团队向导 ===== */}
      <TeamGenModal open={showTeamGen} onClose={() => setShowTeamGen(false)}
        onCreated={async (name) => {
          setMsg(`🎉 新团队「${name}」已创建`);
          await load();
        }} />

      {/* ===== 全局中断确认弹窗 ===== */}
      {mounted && confirmInterrupt && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setConfirmInterrupt(false)}>
          <div className="w-full max-w-md rounded-2xl p-5 space-y-3 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.45)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-bold text-sm text-red-300">🛑 确认全局中断</h3>
            <p className="text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
              将向公司内所有 <b className="text-red-300">{runningTasks.length}</b> 个运行中/排队任务发送停止信号：
              状态置为 interrupted（取消），任务记录与日志完整保留，操作写入审计。
            </p>
            <div className="flex gap-2 pt-1 justify-end">
              <button onClick={() => setConfirmInterrupt(false)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={globalInterrupt} disabled={interrupting}
                className="disabled:opacity-40 px-4 py-2 rounded-xl text-sm font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90 transition-all">
                {interrupting ? "中断中…" : "确认中断全部任务"}
              </button>
            </div>
          </div>
        </div>, document.body
      )}
      </>)}
    </div>
  );
}
