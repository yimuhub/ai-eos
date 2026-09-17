"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import Link from "next/link";
import { api } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import { TASK_STATUS_ZH, zh } from "@/lib/i18n";

/* ============================================================
   任务中心 v2：渐变横幅 · 多维筛选 · 卡片视图 · 生命周期操作
   （与项目页同一设计体系）
   ============================================================ */

type Task = {
  id: string; title: string; status: string; priority: string;
  assigned_agent_id: string | null; description?: string | null; result?: string | null;
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

type Dim = "status" | "priority" | "executor" | "none";

export default function TasksPage() {
  const router = useRouter();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [dim, setDim] = useState<Dim>("status");
  const [dimValue, setDimValue] = useState("全部");
  const [search, setSearch] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  // 批量操作
  const [showBatchDelete, setShowBatchDelete] = useState(false);
  const [batchSel, setBatchSel] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  // 新建任务（仿项目中心）
  const [mounted, setMounted] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [ntForm, setNtForm] = useState({ title: "", description: "", priority: "NORMAL" });
  const [deptGroups, setDeptGroups] = useState<{ dept_id: string; dept_name: string; agents: { id: string; name: string }[] }[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [deptOpen, setDeptOpen] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [ntAttach, setNtAttach] = useState<Attachment[]>([]);
  const [confirmTarget, setConfirmTarget] = useState<{ t: Task } | null>(null);
  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    const q = `/tasks?company_id=${cs[0].id}`;
    const data = await api.get(q);
    setTasks(Array.isArray(data) ? data : []);
    try {
      setAgents(await api.get(`/agents?company_id=${cs[0].id}`));
    } catch {}
    try {
      const ov = await api.get(`/growth-center/departments-overview?company_id=${cs[0].id}`);
      setDeptGroups((ov.departments || []).map((d: any) => ({
        dept_id: d.dept_id, dept_name: d.dept_name,
        agents: (d.agents || []).map((a: any) => ({ id: a.id, name: a.name })),
      })));
    } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

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
      await api.post(`/tasks/${id}/${action}`);
      setMsg(`✅ ${zhLabel[action]}`);
      await load();
    } catch (e: any) { setMsg(`❌ ${e.message || e}`); }
    setBusyId(null);
  }

  /* ---------- 部门化选人（仿项目中心） ---------- */
  const allAgentIds = deptGroups.flatMap((g) => g.agents.map((a) => a.id));
  const isAllPicked = allAgentIds.length > 0 && allAgentIds.every((id) => picked.has(id));
  const toggleAll = () => setPicked(isAllPicked ? new Set() : new Set(allAgentIds));
  const toggleAgent = (id: string) => setPicked((prev) => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
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
      const cid2 = (await api.get("/companies"))[0]?.id;
      const attachNote = ntAttach.length
        ? `\n\n📎 附件：${ntAttach.map((a) => a.name).join("、")}`
        : "";
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/task-dispatch/dispatch`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({
          company_id: cid2,
          title: ntForm.title.trim(),
          description: (ntForm.description || null)
            ? ntForm.description + attachNote : (attachNote ? attachNote.trim() : null),
          priority: ntForm.priority,
          target_type: "multi_agent_list",
          agent_ids: Array.from(picked),
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      setMsg(`✅ 已创建任务「${ntForm.title.trim()}」· 分发给 ${data.created_count} 名成员`);
      setShowCreate(false);
      setNtForm({ title: "", description: "", priority: "NORMAL" });
      setPicked(new Set()); setNtAttach([]);
      await load();
    } catch (e: any) { setMsg(`❌ ${e.message || e}`); }
    setCreating(false);
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
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/task-dispatch/delete`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("aieos_token")}`,
          },
          body: JSON.stringify({ task_ids: [t.id] }),
        });
      const data = await r.json();
      if (data.deleted_count === 1) setMsg("🗑 已彻底删除");
      else setMsg(`❌ 删除失败: ${JSON.stringify(data.failed)}`);
      await load();
    } catch (e: any) { setMsg(`❌ ${e.message}`); }
    setBusyId(null);
  }

  /* ---------- 批量删除（复用 task-dispatch delete API）---------- */
  async function batchDeleteTasks() {
    if (!batchSel.size) return;
    setBatchDeleting(true);
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/task-dispatch/delete`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("aieos_token")}`,
          },
          body: JSON.stringify({ task_ids: Array.from(batchSel) }),
        });
      const data = await r.json();
      setMsg(`🗑 已彻底删除 ${data.deleted_count} 个任务${data.failed.length ? `，失败 ${data.failed.length}` : ""}`);
      setShowBatchDelete(false);
      setBatchSel(new Set());
      await load();
    } catch (e: any) { setMsg(e.message || String(e)); }
    setBatchDeleting(false);
  }

  function toggleBatchSel(id: string) {
    setBatchSel((prev) => {
      const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
    });
  }

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
          <div className="flex items-center gap-2">
            <button onClick={() => { setShowBatchDelete(true); setBatchSel(new Set()); }}
              className="btn-ghost">🗑 批量删除</button>
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
        <input placeholder="搜索…" value={search} onChange={(e) => setSearch(e.target.value)}
          className="input w-40 !py-1.5 text-xs" />
      </div>

      {/* 卡片网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-start">
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
              className={`card group relative overflow-hidden cursor-pointer flex flex-col ${
                inBatch ? "!border-red-500/60" : ""}`}
              style={{
                ...(inBatch ? { boxShadow: "0 0 0 1px rgba(239,68,68,0.4)" } : {}),
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
                      className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                        canPause ? "hover:bg-amber-500/12 text-amber-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                      style={{ border: "1px solid var(--border)" }}>⏸ 暂停</button>
                  )}
                  <button onClick={() => canStop && control(t.id, "cancel")} disabled={!canStop}
                    className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                      canStop ? "hover:bg-orange-500/12 text-orange-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                    style={{ border: "1px solid var(--border)" }}>⏹ 停止</button>
                  <button onClick={() => requestDelete(t)}
                    className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                    style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑 删除</button>
              </div>
            </div>
          );
        })}

        {!filtered.length && (
          <div className="card card-static text-center py-16 col-span-full text-sm" style={{ color: "var(--muted)" }}>
            {search || dimValue !== "全部" ? "没有符合筛选条件的任务" : "暂无任务"}
          </div>
        )}
      </div>

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
                          n.has(g.dept_id) ? n.delete(g.dept_id) : n.add(g.dept_id);
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
                彻底删除任务「{confirmTarget.t.title}」？\n所有记忆、成果、执行记录将全部清除，不可恢复！
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
                <button onClick={() => () => setBatchSel(new Set())} className="text-[11px] hover:text-[var(--foreground)] transition-colors">清空</button>
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
