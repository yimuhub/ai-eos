"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import type { Company } from "@/lib/types";

/* ============================================================
   项目中心 v2：多维分类 · 卡片视图 · 进度可视化 · 生命周期操作
   ============================================================ */

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#6366f1"];

type Project = {
  id: string; name: string; description: string | null;
  status: string; priority: string;
  manager_agent_id: string | null;
  start_date?: string | null; target_date?: string | null;
};
type Agent = { id: string; name: string; role: string | null };
type Dept = { id: string; name: string };
type Progress = { percent: number; total_tasks: number; completed: number; running?: number };

const STATUS_ZH: Record<string, { label: string; cls: string; dot: string }> = {
  DRAFT: { label: "草稿", cls: "text-[var(--muted)]", dot: "bg-gray-500" },
  PLANNING: { label: "规划中", cls: "text-purple-300", dot: "bg-purple-400" },
  ACTIVE: { label: "执行中", cls: "text-cyan-300", dot: "bg-cyan-400" },
  PAUSED: { label: "已暂停", cls: "text-amber-300", dot: "bg-amber-400" },
  REVIEW: { label: "评审中", cls: "text-blue-300", dot: "bg-blue-400" },
  COMPLETED: { label: "已完成", cls: "text-emerald-300", dot: "bg-emerald-400" },
  ARCHIVED: { label: "已归档", cls: "text-[var(--muted)]", dot: "bg-gray-600" },
  CANCELLED: { label: "已停止", cls: "text-red-300", dot: "bg-red-400" },
};
const PRIORITY_ZH: Record<string, string> = {
  LOW: "低", NORMAL: "普通", HIGH: "高", URGENT: "紧急",
};
const DEPT_GRADS = ["#6366f1,#a78bfa", "#22d3ee,#3b82f6", "#f59e0b,#f97316", "#10b981,#14b8a6", "#ec4899,#f43f5e"];
const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

type Dim = "executor" | "department" | "status" | "none";

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [depts, setDepts] = useState<Dept[]>([]);
  const [progressMap, setProgressMap] = useState<Record<string, Progress>>({});
  const [msg, setMsg] = useState("");

  // 多维筛选
  const [dim, setDim] = useState<Dim>("status");
  const [dimValue, setDimValue] = useState("全部");
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", manager_agent_id: "", priority: "NORMAL" });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  const [deptGroups, setDeptGroups] = useState<{ dept_id: string; dept_name: string; agents: { id: string; name: string }[] }[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [deptOpen, setDeptOpen] = useState<Set<string>>(new Set());
  const [projAttach, setProjAttach] = useState<Attachment[]>([]);
  const [confirmTarget, setConfirmTarget] = useState<{ p: Project; action: "pause" | "resume" | "stop" | "delete" } | null>(null);
  const [showBatchDelete, setShowBatchDelete] = useState(false);
  const [batchSel, setBatchSel] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  const load = useCallback(async () => {
    const cs = await api.get<Company[]>("/companies");
    if (!cs.length) return;
    const cid = cs[0].id;
    const ps: Project[] = await api.get(`/projects?company_id=${cid}`);
    setProjects(ps);
    setAgents(await api.get(`/agents?company_id=${cid}`));
    try { setDepts(await api.get(`/companies/${cid}/departments`)); } catch {}
    try {
      const ov = await api.get(`/growth-center/departments-overview?company_id=${cid}`);
      setDeptGroups((ov.departments || []).map((d: any) => ({
        dept_id: d.dept_id, dept_name: d.dept_name,
        agents: (d.agents || []).map((a: any) => ({ id: a.id, name: a.name })),
      })));
    } catch {}
    // 并发拉进度
    const entries = await Promise.all(ps.map(async (p) => {
      try {
        return [p.id, await api.get(`/projects/${p.id}/progress`)] as const;
      } catch { return [p.id, null] as const; }
    }));
    setProgressMap(Object.fromEntries(entries.filter(([, v]) => v)));
  }, []);

  useEffect(() => { load(); }, [load]);

  /* ---------- 筛选维度选项 ---------- */
  const dimOptions = useMemo(() => {
    if (dim === "executor") {
      return ["全部", ...agents.map((a) => a.name)];
    }
    if (dim === "department") {
      // 按项目经理所在部门近似（项目无直接部门字段时按负责人分组）
      return ["全部", ...depts.map((d) => d.name), "未分配部门"];
    }
    if (dim === "status") return ["全部", ...Object.values(STATUS_ZH).map((s) => s.label)];
    return ["全部"];
  }, [dim, agents, depts]);

  // 包成 useCallback：它被下面的 useMemo 依赖，普通函数每次渲染都会换引用，
  // 会让 useMemo 的缓存永远失效（也会让 lint 无法判断依赖是否完整）。
  const projectDeptName = useCallback((p: Project): string => {
    const mgr = agents.find((a) => a.id === p.manager_agent_id);
    if (!mgr) return "未分配部门";
    const d = depts.find((x) => x.id === (mgr as any).department_id);
    return d?.name || "未分配部门";
  }, [agents, depts]);

  const filtered = useMemo(() => {
    let list = [...projects];
    // 排序：置顶逻辑——ACTIVE > PLANNING > PAUSED > 其他
    const order: Record<string, number> = { ACTIVE: 0, PLANNING: 1, REVIEW: 2, PAUSED: 3, TODO: 4, COMPLETED: 5, ARCHIVED: 6, CANCELLED: 7 };
    list.sort((a, b) =>
      ((order[a.status] ?? 9) - (order[b.status] ?? 9)) ||
      a.name.localeCompare(b.name, "zh"));
    if (search) list = list.filter((p) => p.name.includes(search) || (p.description || "").includes(search));
    if (dimValue !== "全部") {
      list = list.filter((p) => {
        if (dim === "executor") {
          const mgr = agents.find((a) => a.id === p.manager_agent_id);
          return mgr?.name === dimValue;
        }
        if (dim === "department") return projectDeptName(p) === dimValue;
        if (dim === "status") return STATUS_ZH[p.status]?.label === dimValue;
        return true;
      });
    }
    return list;
  }, [projects, search, dimValue, dim, agents, projectDeptName]);

  async function batchDeleteProjects() {
    if (!batchSel.size) return;
    setBatchDeleting(true);
    try {
      const data = await api.post<{ deleted_count: number; failed: string[] }>(
        "/projects/batch-delete", { project_ids: Array.from(batchSel) });
      setMsg(`🗑 已彻底删除 ${data.deleted_count} 个项目${data.failed.length ? `，失败 ${data.failed.length}` : ""}`);
      setShowBatchDelete(false);
      setBatchSel(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "批量删除失败")}`); }
    setBatchDeleting(false);
  }

  /* ---------- 生命周期操作 ---------- */
  function lifecycle(p: Project, action: "pause" | "resume" | "stop" | "delete") {
    setConfirmTarget({ p, action });
  }

  async function executeLifecycle() {
    if (!confirmTarget) return;
    const { p, action } = confirmTarget;
    setConfirmTarget(null);
    setBusyId(p.id);
    try {
      await api.post(`/projects/${p.id}/lifecycle`, { action });
      const zhLabel = { pause: "已暂停（现场保留）", resume: "已恢复执行", stop: "已永久停止（成果保留）", delete: "已彻底删除" };
      setMsg(`✅ 「${p.name}」${zhLabel[action]}`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
    setBusyId(null);
  }

  /* ---------- 创建项目 ---------- */
  /* ---------- 部门化选人 ---------- */
  const allAgentIds = deptGroups.flatMap((g) => g.agents.map((a) => a.id));
  const isAllPicked = allAgentIds.length > 0 && allAgentIds.every((id) => picked.has(id));
  const toggleAll = () => setPicked(isAllPicked ? new Set() : new Set(allAgentIds));
  const toggleAgent = (id: string) => setPicked((prev) => {
    const n = new Set(prev);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });
  const deptAgentIdsOf = (g: { agents: { id: string }[] }) => g.agents.map((a) => a.id);
  const isDeptAllPicked = (g: { agents: { id: string }[] }) => deptAgentIdsOf(g).every((id) => picked.has(id));
  const toggleDept = (g: { agents: { id: string }[] }) => {
    const ids = deptAgentIdsOf(g);
    const all = ids.every((id) => picked.has(id));
    setPicked((prev) => {
      const n = new Set(prev);
      ids.forEach((id) => (all ? n.delete(id) : n.add(id)));
      return n;
    });
  };

  async function createProject() {
    if (!form.name.trim()) return;
    try {
      const cs = await api.get<Company[]>("/companies");
      const created = await api.post(`/projects?company_id=${cs[0].id}`, {
        name: form.name.trim(),
        description: form.description || null,
        manager_agent_id: form.manager_agent_id || null,
        priority: form.priority,
      });
      // 批量添加项目成员
      const pid = created?.id || created?.project?.id;
      if (pid && picked.size) {
        for (const mid of Array.from(picked)) {
          try {
            await api.post(`/projects/${pid}/members`, {
              member_type: "agent", member_id: mid, role_in_project: "member",
            });
          } catch {}
        }
      }
      setMsg(`✅ 项目「${form.name}」已创建${picked.size ? ` · ${picked.size} 名成员已加入` : ""}`);
      setShowForm(false);
      setForm({ name: "", description: "", manager_agent_id: "", priority: "NORMAL" });
      setPicked(new Set()); setProjAttach([]);
      await load();
    } catch (e: any) { setMsg(e.message || String(e)); }
  }

  const agentName = (id: string | null) =>
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
            <h1 className="text-2xl font-bold tracking-tight">项目</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              共 {projects.length} 个项目 · 多维分类 · 生命周期管理
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowBatchDelete(true)} className="btn-ghost">🗑 批量删除</button>
            <button onClick={() => setShowForm(!showForm)} className="btn-primary">＋ 新建项目</button>
          </div>
        </div>
      </div>

      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {/* 新建表单 */}
      {showForm && (
        <div className="card mb-5 space-y-2.5 animate-fade-up">
          <h3 className="font-medium text-sm">新建项目</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
            <input placeholder="项目名称 *" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <select value={form.manager_agent_id}
              onChange={(e) => setForm({ ...form, manager_agent_id: e.target.value })} className="input">
              <option value="">选择负责 Agent（可选）</option>
              {agents.map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
            </select>
            <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="input">
              <option value="LOW">优先级：低</option>
              <option value="NORMAL">优先级：普通</option>
              <option value="HIGH">优先级：高</option>
              <option value="URGENT">优先级：紧急</option>
            </select>
          </div>
          <textarea rows={2} placeholder="项目描述" value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })} className="input" />

          {/* 附件 */}
          <AttachmentPicker value={projAttach} onChange={setProjAttach} label="添加项目附件" />

          {/* 部门化选人 */}
          <div>
            <p className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: "var(--muted)" }}>
              👥 选择项目成员（按部门 · 已选 {picked.size} 人）
              {picked.size > 0 && (
                <button onClick={() => setPicked(new Set())}
                  className="text-red-300 hover:bg-red-500/10 px-2 py-0.5 rounded-md text-[11px]">清空</button>
              )}
            </p>
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {/* 全员参加：一键全选 */}
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
                    <span className="text-sm font-semibold block">全员项目</span>
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
                const grad = AVATAR_COLORS[Math.abs(
                  g.dept_name.split("").reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_COLORS.length];
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

          <button onClick={createProject} disabled={!form.name.trim()}
            className="btn-primary disabled:opacity-40">
            创建项目{picked.size ? `（${picked.size} 名成员）` : ""}
          </button>
        </div>
      )}

      {/* 多维筛选栏 */}
      <div className="card card-static mb-5 flex items-center gap-3 flex-wrap !py-3">
        <span className="text-xs font-medium shrink-0">分类维度：</span>
        {([["status", "按状态"], ["executor", "按执行 Agent"], ["department", "按部门"]] as const).map(([v, label]) => (
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

      {/* 卡片墙 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-start">
        {filtered.map((p, i) => {
          const st = STATUS_ZH[p.status] || STATUS_ZH.DRAFT;
          const prog = progressMap[p.id];
          const pct = prog?.percent ?? 0;
          const mgr = agentName(p.manager_agent_id);
          const grad = AVATAR_GRADS[i % AVATAR_GRADS.length];
          const isBusy = busyId === p.id;
          const canPause = p.status === "ACTIVE";
          const canResume = p.status === "PAUSED";
          const canStop = !["COMPLETED", "CANCELLED", "ARCHIVED"].includes(p.status);

          return (
            <div key={p.id} className={`card group relative overflow-hidden cursor-pointer flex flex-col ${
              showBatchDelete && batchSel.has(p.id) ? "!border-red-500/60" : ""}`}
              onClick={() => showBatchDelete
                ? setBatchSel((prev) => {
                    const n = new Set(prev); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n;
                  })
                : router.push(`/projects/${p.id}`)}>
              <div className="absolute top-0 left-0 w-full h-[2px]"
                style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />

              {isBusy && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/50 text-xs text-indigo-200">
                  处理中…
                </div>
              )}

              {/* 批量删除模式的勾选框 */}
              {showBatchDelete && (
                <label className="absolute top-3 right-3 z-10 cursor-pointer"
                  onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={batchSel.has(p.id)}
                    onChange={() => setBatchSel((prev) => {
                      const n = new Set(prev); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n;
                    })}
                    className="accent-red-500 w-4 h-4" />
                </label>
              )}

              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                  style={{ background: `linear-gradient(135deg, ${grad})`, boxShadow: `0 4px 14px -6px ${grad.split(",")[0]}66` }}>
                  📊
                </div>
                <div className="min-w-0 flex-1 pr-12">
                  <div className="flex items-center gap-1.5">
                    <span className="font-semibold text-[15px] truncate">{p.name}</span>
                    {p.priority === "URGENT" && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 shrink-0">紧急</span>
                    )}
                  </div>
                  <div className="mt-1 inline-flex items-center gap-1.5 text-[11px]" >
                    <span className={`w-1.5 h-1.5 rounded-full ${st.dot} ${p.status === "ACTIVE" ? "pulse-dot" : ""}`} />
                    <span className={st.cls}>{st.label}</span>
                    <span style={{ color: "var(--muted)" }}>· {PRIORITY_ZH[p.priority] || p.priority}优先级</span>
                  </div>
                </div>
              </div>

              {/* 描述 */}
              <p className="text-xs mt-2 leading-relaxed line-clamp-2" style={{ color: "var(--muted)" }}>
                {p.description || "暂无描述"}
              </p>

              {/* 进度条 */}
              <div className="mt-3">
                <div className="flex justify-between items-center mb-1">
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>项目进度</span>
                  <span className="text-[11px] font-bold tabular-nums text-gradient">{pct}%</span>
                </div>
                <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
                  <div className="h-full rounded-full transition-all duration-500"
                    style={{ width: `${pct}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }} />
                </div>
                {prog && (
                  <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                    任务 {prog.completed}/{prog.total_tasks} · 运行中 {prog.running}
                  </p>
                )}
              </div>

              {/* 执行 Agent */}
              <div className="mt-2.5 flex items-center gap-2 min-h-7">
                <span className="text-[10px] shrink-0" style={{ color: "var(--muted)" }}>负责:</span>
                {mgr ? (
                  <>
                    <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                      style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[(i + 1) % AVATAR_GRADS.length]})` }}>
                      {mgr.slice(0, 1)}
                    </div>
                    <span className="text-xs truncate">{mgr}</span>
                  </>
                ) : (
                  <span className="text-xs" style={{ color: "var(--muted)" }}>未指定</span>
                )}
              </div>

              {/* 悬停操作 */}
              <div className="mt-auto pt-3 mt-3 border-t flex gap-1.5 opacity-0 group-hover:opacity-100 transition-all duration-200" style={{ borderColor: "var(--border)" }} onClick={(e) => e.stopPropagation()} hidden={false}>
                  {canResume ? (
                    <button onClick={() => lifecycle(p, "resume")}
                      className="flex-1 text-[11px] py-1.5 rounded-lg hover:bg-emerald-500/12 text-emerald-300 transition-colors"
                      style={{ border: "1px solid rgba(16,185,129,0.3)" }}>▶ 恢复</button>
                  ) : (
                    <button onClick={() => canPause && lifecycle(p, "pause")} disabled={!canPause}
                      className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                        canPause ? "hover:bg-amber-500/12 text-amber-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                      style={{ border: "1px solid var(--border)" }}>⏸ 暂停</button>
                  )}
                  <button onClick={() => canStop && lifecycle(p, "stop")} disabled={!canStop}
                    className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                      canStop ? "hover:bg-orange-500/12 text-orange-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                    style={{ border: "1px solid var(--border)" }}>⏹ 停止</button>
                  <button onClick={() => lifecycle(p, "delete")}
                    className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                    style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑 删除</button>
                </div>
            </div>
          );
        })}

        {!filtered.length && (
          <div className="card card-static text-center py-16 col-span-full text-sm" style={{ color: "var(--muted)" }}>
            {search || dimValue !== "全部" ? "没有符合筛选条件的项目" : "还没有项目 · 点击右上角「新建项目」开始"}
          </div>
        )}
      </div>

      {/* 生命周期确认弹窗（Portal，替代原生 confirm） */}
      {mounted && confirmTarget && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setConfirmTarget(null)}>
          <div className="w-full max-w-sm max-h-[90vh] overflow-y-auto rounded-2xl p-5 space-y-4 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: confirmTarget.action === "delete" ? "1px solid rgba(239,68,68,0.45)" : "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3">
              <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                style={{ background: confirmTarget.action === "delete" ? "linear-gradient(135deg,#ef4444,#f59e0b)" : "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
                {confirmTarget.action === "delete" ? "⚠️" : confirmTarget.action === "pause" ? "⏸" : confirmTarget.action === "resume" ? "▶" : "⏹"}
              </span>
              <p className="text-sm leading-relaxed whitespace-pre-wrap flex-1">
                {{ pause: `暂停项目「${confirmTarget.p.name}」？\n运行中任务将挂起待命，上下文与数据完整保留。`,
                   resume: `恢复项目「${confirmTarget.p.name}」？\n将还原全部上下文，从暂停处继续执行。`,
                   stop: `永久停止项目「${confirmTarget.p.name}」？\n已有成果与记忆全部保留，停止后无法自动恢复。`,
                   delete: `彻底删除项目「${confirmTarget.p.name}」？\n所有关联任务、记忆、成果、记录将被清除，不可恢复！` }[confirmTarget.action]}
              </p>
            </div>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmTarget(null)}
                className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={executeLifecycle}
                className={`px-4 py-2 rounded-xl text-xs font-medium text-white transition-all ${confirmTarget.action === "delete"
                  ? "bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90"
                  : "bg-gradient-to-r from-indigo-600 to-violet-500 hover:opacity-90"}`}>
                {confirmTarget.action === "delete" ? "确认彻底删除" :
                 confirmTarget.action === "pause" ? "确认暂停" :
                 confirmTarget.action === "resume" ? "确认恢复" : "确认停止"}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 批量删除弹窗 */}
      {mounted && showBatchDelete && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => { setShowBatchDelete(false); setBatchSel(new Set()); }}>
          <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-2xl overflow-hidden animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(248,113,113,0.25)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="px-5 pt-5 pb-3 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <div>
                <h3 className="font-bold text-sm">🗑 批量删除项目</h3>
                <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  已选 {batchSel.size} 个（当前筛选范围 {filtered.length} 个）· 关联任务/记忆/成果将全部清除
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
            <div className="max-h-[50vh] overflow-y-auto p-4 grid grid-cols-3 gap-2">
              {filtered.map((pj, pi) => {
                const on = batchSel.has(pj.id);
                return (
                  <button key={pj.id} type="button"
                    onClick={() => setBatchSel((prev) => {
                      const n = new Set(prev); if (n.has(pj.id)) n.delete(pj.id); else n.add(pj.id); return n;
                    })}
                    className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${
                      on ? "border-red-500/60 bg-red-500/10" : "hover:bg-white/[0.04]"}`}
                    style={on ? {} : { borderColor: "var(--border)" }}>
                    <div className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs shrink-0"
                      style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[pi % AVATAR_GRADS.length]})` }}>📊</div>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium truncate">{pj.name}</div>
                      <div className="text-[10px]" style={{ color: "var(--muted)" }}>
                        {STATUS_ZH[pj.status]?.label || pj.status}
                      </div>
                    </div>
                    {on && <span className="text-red-400 text-xs shrink-0">✓</span>}
                  </button>
                );
              })}
            </div>
            <div className="px-5 py-4 border-t flex items-center gap-3" style={{ borderColor: "var(--border)" }}>
              <button onClick={batchDeleteProjects} disabled={!batchSel.size || batchDeleting}
                className="disabled:opacity-40 px-5 py-2 rounded-lg text-sm font-medium text-white transition-all"
                style={{
                  background: "linear-gradient(135deg,#ef4444,#f97316)",
                  boxShadow: "0 2px 12px -2px rgba(239,68,68,0.5)",
                }}>
                {batchDeleting ? "删除中…" : `确认删除 ${batchSel.size} 个项目`}
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
