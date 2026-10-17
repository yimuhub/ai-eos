"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { invalidate, useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import Field from "@/components/Field";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";
import { useFormValidation } from "@/lib/useFormValidation";
import { V } from "@/lib/validate";
import { notify } from "@/components/Toast";
import { AGENT_STATUS_ZH, zh } from "@/lib/i18n";
import type {
  Agent, CapabilityCatalog, CapabilityPack, AgentBackend,
  Department as Dept, ModelConfig as ModelRow,
} from "@/lib/types";

const STATUS_STYLE: Record<string, { cls: string; dot: string }> = {
  IDLE: { cls: "text-gray-300", dot: "bg-gray-500" },
  RUNNING: { cls: "text-cyan-300", dot: "bg-cyan-400" },
  PAUSED: { cls: "text-amber-300", dot: "bg-amber-400" },
  ERROR: { cls: "text-red-300", dot: "bg-red-400" },
  DISABLED: { cls: "text-[var(--muted)]", dot: "bg-white/[0.25]" },
  ACTIVE: { cls: "text-emerald-300", dot: "bg-emerald-400" },
};
const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

export default function AgentsPage() {
  const router = useRouter();
  const cid = useActiveCompanyId();
  const [filter, setFilter] = useState("ALL");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showForm, setShowForm] = useState(false);
  const [showBatchForm, setShowBatchForm] = useState(false);
  const [msg, setMsg] = useState("");
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  const [form, setForm] = useState({ name: "", role: "", department_id: "", system_prompt: "", model_id: "", backend: "native_llm" });
  const [selCaps, setSelCaps] = useState<Set<string>>(new Set());
  const [agentAttachments, setAgentAttachments] = useState<Attachment[]>([]); // 📎 新建员工附件口
  // 批量创建
  const [batchText, setBatchText] = useState("");
  const [batchDept, setBatchDept] = useState("");
  const [batchMgr, setBatchMgr] = useState("");
  const [batchModel, setBatchModel] = useState("");
  // V5.0 专家库联动：招聘 / 批量 都可从专家库直接选专家员工
  type ExpertRow = {
    id: string; name: string; emoji?: string | null;
    department: string; description?: string;
    recommended_packs?: string[];
  };
  const [experts, setExperts] = useState<ExpertRow[]>([]);
  const [expertSel, setExpertSel] = useState("");       // 单个招聘时选中的专家
  const [expertSearch, setExpertSearch] = useState("");  // 专家搜索框
  const [batchExperts, setBatchExperts] = useState<Set<string>>(new Set()); // 批量勾选的专家
  const [batchMode, setBatchMode] = useState<"text" | "experts">("text");
  // 编辑弹窗
  const [editing, setEditing] = useState<Agent | null>(null);
  const [editForm, setEditForm] = useState({
    role: "", department_id: "", manager_agent_id: "",
    system_prompt: "", backend: "native_llm",
    capabilities: new Set<string>(), model_id: "",
    status: "IDLE",
  });
  const [saving, setSaving] = useState(false);
  // 组织视图折叠的部门
  const [collapsedDepts, setCollapsedDepts] = useState<Set<string>>(new Set());
  // 置顶的部门与 Agent（localStorage 持久化）
  const [pinnedDepts, setPinnedDepts] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("aieos_pinned_depts") || "[]"); } catch { return []; }
  });
  const [pinnedAgents, setPinnedAgents] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("aieos_pinned_agents") || "[]"); } catch { return []; }
  });
  useEffect(() => { localStorage.setItem("aieos_pinned_depts", JSON.stringify(pinnedDepts)); }, [pinnedDepts]);
  useEffect(() => { localStorage.setItem("aieos_pinned_agents", JSON.stringify(pinnedAgents)); }, [pinnedAgents]);
  function togglePinDept(name: string) {
    setPinnedDepts((prev) => prev.includes(name) ? prev.filter((x) => x !== name) : [name, ...prev]);
  }
  function togglePinAgent(id: string) {
    setPinnedAgents((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [id, ...prev]);
  }

  /* ---------- 数据加载 ----------
   *
   * 改造前是 4 个请求**串行**发出（部门 → 模型 → 员工 → 能力目录），
   * 每个都要等上一个回来，员工越多越慢；且没有缓存，每次进页面从零拉。
   * 现在交给统一数据层：并发去重、缓存首屏、聚焦重取。
   */
  const {
    data: catalog, error: loadError, refresh,
  } = useResource(
    cid ? `agents:${cid}` : null,
    async () => {
      // 四个接口互不依赖 → 并发发出，总耗时从"四段之和"降到"最慢的一段"
      const [depts, models, agents, caps] = await Promise.all([
        api.get<Dept[]>(`/companies/${cid}/departments`),
        api.get<ModelRow[]>("/models"),
        api.get<Agent[]>(`/agents?company_id=${cid}`),
        api.get<CapabilityCatalog>("/agents/capability-packs"),
      ]);
      return { depts, models, agents, capPacks: caps.packs || [], backends: caps.backends || [] };
    },
  );

  // 派生数据统一 useMemo：`?? []` 每次渲染都是一个新数组，
  // 会让下游 useMemo 的依赖每次都变，等于缓存失效。
  const agents = useMemo<Agent[]>(() => catalog?.agents ?? [], [catalog]);
  const depts = useMemo<Dept[]>(() => catalog?.depts ?? [], [catalog]);
  const models = useMemo<ModelRow[]>(() => catalog?.models ?? [], [catalog]);
  const capPacks = useMemo<CapabilityPack[]>(() => catalog?.capPacks ?? [], [catalog]);
  const backends = useMemo<AgentBackend[]>(() => catalog?.backends ?? [], [catalog]);

  /** 员工数据变了 → 让所有引用 Agent 列表的页面一起失效，再重取本页 */
  const load = useCallback(async () => {
    invalidate("agents:");
    invalidate("models:");
    await refresh();
  }, [refresh]);

  /* ---------- V5.0 专家库：加载 + 选择回填 ---------- */
  const loadExperts = useCallback(() => {
    if (!cid) return;
    api.get<{ id: string; name: string; emoji?: string | null; department: string; description?: string }[]>(
      "/experts?company_id=" + cid)
      .then((rows) => setExperts(rows || []))
      .catch(() => {});
  }, [cid]);
  useEffect(() => { loadExperts(); }, [loadExperts]);

  /** 单个招聘：选中某专家 → 自动回填 姓名/岗位/人设/部门/技能包 */
  function pickExpert(id: string) {
    setExpertSel(id);
    const ex = experts.find((e) => e.id === id);
    if (ex) {
      setForm((f) => ({
        ...f,
        name: f.name || ex.name,
        role: f.role || ex.name,
        department_id: ex.department || f.department_id,
        system_prompt: f.system_prompt, // 后端会用专家 system_prompt 回填（留空时）
      }));
      // V5.0.2 专家↔技能包：预勾选该专家的推荐技能包（用户仍可手动增减）
      if (ex.recommended_packs?.length) setSelCaps(new Set(ex.recommended_packs));
    }
  }

  /** 批量专家 → 组装批量创建 items（带 expert_template_id，后端回填人设） */
  function batchExpertItems() {
    return experts
      .filter((e) => batchExperts.has(e.id))
      .map((e) => ({
        name: e.name, role: e.name,
        department_id: undefined,
        expert_template_id: e.id,
      }));
  }

  const shown = useMemo(
    () => (filter === "ALL" ? agents : agents.filter((a) => a.status === filter)),
    [agents, filter]);

  /* ---------- 按组织架构分组：顶层(无上级) → 各自下属树 ---------- */
  const orgGroups = useMemo(() => {
    // 部门名映射
    const deptName = (id: string | null) =>
      depts.find((d) => d.id === id)?.name || "未分配部门";

    const byId = new Map(shown.map((a) => [a.id, a]));
    const childrenMap: Record<string, Agent[]> = {};
    const roots: Agent[] = [];
    for (const a of shown) {
      if (a.manager_agent_id && byId.has(a.manager_agent_id)) {
        (childrenMap[a.manager_agent_id] ||= []).push(a);
      } else {
        roots.push(a);
      }
    }
    return { roots, childrenMap, deptName };
  }, [shown, depts]);

  /* ---------- 多选 ---------- */
  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }
  async function batchDelete() {
    if (!selected.size) return;
    if (!(await confirmAction({
      title: `确认删除选中的 ${selected.size} 个 Agent？`,
      description: "这会一次性销毁多名员工的全部痕迹，请先确认选中的范围。",
      bullets: [
        `共 ${selected.size} 名员工及其档案`,
        "他们的记忆、成果与执行记录一并清除",
        "此操作不可恢复",
      ],
      // 批量删除是"一次点击销毁多人"，用输入确认词把"手滑"和"确认"真正区分开
      confirmPhrase: "删除",
      confirmText: "删除",
    }))) return;
    try {
      const r = await api.post<{ deleted_count: number; failed: string[] }>(
        "/agents/batch-delete", { agent_ids: Array.from(selected) });
      setMsg(`✅ 已删除 ${r.deleted_count} 个${r.failed.length ? `，${r.failed.length} 个失败` : ""}`);
      setSelected(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "批量删除失败")}`); }
  }
  async function batchPause() {
    if (!selected.size) return;
    for (const id of Array.from(selected)) {
      await api.patch(`/agents/${id}`, { status: "PAUSED" }).catch(() => {});
    }
    setMsg(`✅ 已暂停 ${selected.size} 个`);
    await load();
  }
  async function batchResume() {
    if (!selected.size) return;
    for (const id of Array.from(selected)) {
      await api.patch(`/agents/${id}`, { status: "IDLE" }).catch(() => {});
    }
    setMsg(`✅ 已恢复 ${selected.size} 个`);
    setSelected(new Set());
    await load();
  }

  /* ---------- 单个创建 ---------- */
  //
  // 校验规则集中在这里声明，避免"每个字段各写一句 if"散落各处。
  // 关键是**给出原因**：原来的 `if (!form.name) return;` 点了没反应，
  // 用户只能猜是网络问题还是自己漏填了什么。
  const agentForm = useFormValidation<{ name: string; role: string; system_prompt: string }>(
    useMemo(
      () => ({
        name: [V.required("姓名"), V.maxLen(50, "姓名")],
        role: [V.maxLen(50, "岗位")],
        system_prompt: [V.maxLen(4000, "人设与职责设定")],
      }),
      [],
    ),
  );

  async function createAgent() {
    const values = { name: form.name, role: form.role, system_prompt: form.system_prompt };
    if (!agentForm.check(values)) {
      notify("warning", agentForm.first() || "请检查填写内容");
      return;
    }
    try {
      const selExpert = expertSel ? experts.find((e) => e.id === expertSel) : undefined;
      const res = await api.post<{ id: string }>("/agents", {
        company_id: cid, name: form.name, role: form.role || null,
        department_id: form.department_id || null,
        system_prompt: form.system_prompt || null,
        capabilities: Array.from(selCaps),
        backend: form.backend,
        expert_template_id: selExpert?.id || null,  // V5.0：绑定专家，后端自动回填人设
        attachments: agentAttachments.length ? agentAttachments : undefined, // 📎 附件口
      });
      if (form.model_id) {
        await api.post(`/agents/${res.id}/models`, { role: "primary", model_id: form.model_id });
      }
      notify("success", `已创建员工「${form.name}」`);
      setShowForm(false);
      setForm({ name: "", role: "", department_id: "", system_prompt: "", model_id: "", backend: "native_llm" });
      setExpertSel(""); setExpertSearch("");
      setSelCaps(new Set());
      setAgentAttachments([]);
      agentForm.reset();
      await load();
    } catch (e) {
      // 原来创建失败时异常会直接冒泡成 unhandled rejection：用户看不到任何提示
      notify("error", `创建失败：${describeError(e, "创建失败")}`);
    }
  }

  /* ---------- 批量创建 ---------- */
  async function batchCreate() {
    // V5.0 双模式：专家勾选 或 手动输入
    let items: { name: string; role?: string; expert_template_id?: string; department_id?: string }[];
    if (batchMode === "experts") {
      items = batchExpertItems();
    } else {
      items = batchText.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
        const [name, role] = line.split(/[,，]/).map((s) => s.trim());
        return { name, role: role || undefined };
      }).filter((i) => i.name);
    }
    if (!items.length) { setMsg("⚠️ " + (batchMode === "experts" ? "请勾选至少一位专家" : "请填写至少一名员工")); return; }
    try {
      const r = await api.post<{ created_count: number }>("/agents/batch-create", {
        company_id: cid,
        department_id: batchDept || undefined,
        manager_agent_id: batchMgr || undefined,
        model_id: batchModel || undefined,
        agents: items,
      });
      setMsg(`✅ 批量创建 ${r.created_count} 名员工${batchMode === "experts" ? "（来自专家库）" : ""}`);
      setShowBatchForm(false);
      setBatchText("");
      setBatchExperts(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "批量创建失败")}`); }
  }

  /* ---------- 编辑弹窗 ---------- */
  function openEdit(a: Agent) {
    setEditing(a);
    setEditForm({
      role: a.role || "",
      department_id: a.department_id || "",
      manager_agent_id: a.manager_agent_id || "",
      system_prompt: a.system_prompt || "",
      backend: a.memory_config?.backend || "native_llm",
      capabilities: new Set(a.memory_config?.capabilities || []),
      model_id: a.model_configs?.find((m) => m.role === "primary")?.model_id || "",
      status: a.status,
    });
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    try {
      // 基础字段 + 能力/底座
      await api.patch(`/agents/${editing.id}`, {
        role: editForm.role || null,
        department_id: editForm.department_id || null,
        manager_agent_id: editForm.manager_agent_id || null,
        system_prompt: editForm.system_prompt || null,
        capabilities: Array.from(editForm.capabilities),
        backend: editForm.backend,
      });
      // 状态
      if (editForm.status !== editing.status) {
        await api.patch(`/agents/${editing.id}`, { status: editForm.status });
      }
      // 模型切换
      const currentModel = editing.model_configs?.find((m) => m.role === "primary")?.model_id;
      if (editForm.model_id && editForm.model_id !== currentModel) {
        // 走统一 api 层：自动带鉴权与超时，失败会抛出可读原因。
        // 原实现是裸 fetch 且**不看返回值**——切换失败也照样提示"已更新"。
        await api.put(`/agents/${editing.id}/switch-model`, { model_id: editForm.model_id });
      }
      setMsg(`✅ ${editing.name} 已更新`);
      setEditing(null);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "保存失败")}`); }
    setSaving(false);
  }

  async function deleteOne(a: Agent) {
    if (!(await confirmAction({
      title: `确认删除 Agent「${a.name}」？`,
      description: "该员工的档案、记忆、成果与执行记录会一并清除。",
      confirmText: "删除",
    }))) return;
    try {
      await api.del(`/agents/${a.id}`);
      setMsg(`✅ ${a.name} 已删除`);
      setEditing(null);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "删除失败")}`); }
  }

  /* ---------- 渲染单个卡片（复用） ---------- */
  function renderCard(a: Agent, depth: number = 0) {
    const st = STATUS_STYLE[a.status] || STATUS_STYLE.IDLE;
    const gradIdx = agents.findIndex((x) => x.id === a.id) % AVATAR_COLORS.length;
    const grad = AVATAR_COLORS[gradIdx < 0 ? 0 : gradIdx];
    const isSel = selected.has(a.id);
    const mngr = a.manager_agent_id
      ? agents.find((x) => x.id === a.manager_agent_id)?.name : null;

    return (
      <div key={a.id}>
        <div className={`card group relative overflow-hidden cursor-pointer ${isSel ? "!border-indigo-500/70" : ""}`}
          style={{
            ...(isSel ? { boxShadow: "0 0 0 1px rgba(99,102,241,0.5), 0 4px 20px -6px rgba(99,102,241,0.35)" } : {}),
            marginLeft: depth * 20,
          }}
          onClick={() => router.push(`/agents/${a.id}`)}>
          <div className="absolute top-0 left-0 w-full h-[2px] opacity-60 group-hover:opacity-100 transition-opacity"
            style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />

          {/* 置顶标记 */}
          {pinnedAgents.includes(a.id) && (
            <span className="absolute top-1.5 left-2 text-[10px] text-amber-300">📌</span>
          )}
          {/* hover 置顶按钮 */}
          <button
            onClick={(e) => { e.stopPropagation(); togglePinAgent(a.id); }}
            title={pinnedAgents.includes(a.id) ? "取消置顶" : "置顶"}
            className={`absolute top-1.5 right-8 z-10 text-[10px] px-1.5 py-0.5 rounded-md transition-all ${pinnedAgents.includes(a.id)
              ? "text-amber-300 bg-amber-500/15 opacity-100" : "opacity-0 group-hover:opacity-100 hover:bg-white/[0.08]"}`}
            style={!pinnedAgents.includes(a.id) ? { color: "var(--muted)" } : {}}>
            📌
          </button>

          {/* 选择框：仅当已有选中项时显示 */}
          {selected.size > 0 && (
            <label className="absolute top-2 right-2 z-10 cursor-pointer" onClick={(e) => e.stopPropagation()}>
              <input type="checkbox" checked={isSel} onChange={() => toggleSelect(a.id)}
                className="accent-indigo-500 w-4 h-4" />
            </label>
          )}

          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-xl flex items-center justify-center text-white text-base font-bold shrink-0"
              style={{ background: `linear-gradient(135deg, ${grad})` }}>
              {a.name.slice(0, 1)}
            </div>
            <div className="min-w-0 flex-1 pr-6">
              <div className="font-medium text-sm truncate">{a.name}</div>
              <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>{a.role || "未设岗位"}</div>
            </div>
          </div>

          {(mngr || a.memory_config?.capabilities?.length) && (
            <div className="mt-2 flex gap-1 flex-wrap items-center">
              {mngr && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-md"
                  style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                  ↳ {mngr}
                </span>
              )}
              {a.memory_config?.capabilities?.map((c) => {
                const pack = capPacks.find((x) => x.key === c);
                return (
                  <span key={c} className="text-[9px] px-1.5 py-0.5 rounded-md"
                    style={{ background: "rgba(16,185,129,0.12)", color: "#6ee7b7" }}>
                    {pack?.label || c}
                  </span>
                );
              })}
              {a.memory_config?.backend && a.memory_config.backend !== "native_llm" && (
                <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-indigo-500/20 text-indigo-300">
                  {a.memory_config.backend === "hermes" ? "Hermes" : a.memory_config.backend === "codex" ? "Codex" : a.memory_config.backend}
                </span>
              )}
            </div>
          )}

          <div className="mt-2.5 flex items-center justify-between">
            <span className={`inline-flex items-center gap-1.5 text-[11px] ${st.cls}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${a.status === "RUNNING" ? "pulse-dot" : ""} ${st.dot}`} />
              {zh(AGENT_STATUS_ZH, a.status)}
            </span>
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] px-1.5 py-0.5 rounded-md" style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                模型 ×{a.model_configs?.length ?? 0}
              </span>
              {/* 悬停编辑按钮（点击不进入对话，打开设置弹窗） */}
              <button title="编辑员工设置"
                onClick={(e) => { e.stopPropagation(); openEdit(a); }}
                className="opacity-0 group-hover:opacity-100 transition-opacity text-[10px] px-2 py-0.5 rounded-md hover:bg-indigo-500/15 text-indigo-300">
                ✎ 编辑
              </button>
            </div>
          </div>
        </div>

        {/* 下属缩进展示 */}
        {orgGroups.childrenMap[a.id]?.map((child) => renderCard(child, depth + 1))}
      </div>
    );
  }

  /* ---------- 按部门分组的顶层员工 ---------- */
  const groupedRoots = useMemo(() => {
    const groups: Record<string, Agent[]> = {};
    for (const a of orgGroups.roots) {
      const key = orgGroups.deptName(a.department_id);
      (groups[key] ||= []).push(a);
    }
    return groups;
  }, [orgGroups]);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">数字员工</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            共 {agents.length} 名 · 点击卡片进入对话 · 悬停卡片可编辑 · 支持上下级汇报关系
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <select value={filter} onChange={(e) => setFilter(e.target.value)} className="input w-auto text-xs py-1.5">
            {["ALL", "ACTIVE", "IDLE", "RUNNING", "PAUSED", "ERROR"].map((s) => (
              <option key={s} value={s}>{s === "ALL" ? "全部状态" : zh(AGENT_STATUS_ZH, s)}</option>
            ))}
          </select>
          <button onClick={() => setShowBatchForm(!showBatchForm)} className="btn-ghost">⚡ 批量添加</button>
          <button onClick={() => router.push("/agent-offboarding")} className="btn-ghost">🗂 离职交接</button>
          <button onClick={() => setShowForm(!showForm)} className="btn-primary">＋ 招新员工</button>
        </div>
      </div>

      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {/* 加载失败不该让页面停在"没有 Agent"的空白上——那会让用户以为员工被删光了 */}
      {loadError && (
        <div className="mb-3 flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
          <span>🔌</span>
          <span className="flex-1">加载员工列表失败：{loadError}</span>
          <button onClick={() => refresh()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

      {/* 单个创建表单 */}
      {showForm && (
        <div className="card mb-4 space-y-2.5">
          <h3 className="font-medium text-sm mb-1">招新员工</h3>

          {/* V5.0 从专家库直接选专家员工（自动回填姓名/岗位/人设） */}
          {experts.length > 0 && (
            <div className="rounded-xl p-3 border border-[var(--border)] bg-white/[0.02] space-y-2">
              <p className="text-xs font-medium text-indigo-300">📚 从专家库选择（自动回填人设）</p>
              <input value={expertSearch} onChange={(e) => setExpertSearch(e.target.value)}
                placeholder={`搜索 ${experts.length} 位专家（如：数据 / 营销 / 测试）…`} className="input text-xs" />
              <div className="grid grid-cols-3 gap-1.5 max-h-36 overflow-y-auto pr-1">
                {experts
                  .filter((ex) => !expertSearch.trim() ||
                    ex.name.toLowerCase().includes(expertSearch.toLowerCase()) ||
                    (ex.description || "").toLowerCase().includes(expertSearch.toLowerCase()))
                  .slice(0, 30).map((ex) => {
                    const on = expertSel === ex.id;
                    return (
                      <button key={ex.id} type="button"
                        onClick={() => pickExpert(on ? "" : ex.id)}
                        className={`text-left px-2.5 py-1.5 rounded-lg text-xs transition-all border ${
                          on ? "border-emerald-400/60 bg-emerald-500/15" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "var(--border)" }}>
                        <span className="font-medium">{ex.emoji || "🧑‍💼"} {ex.name}</span>
                        <div className="text-[10px] opacity-60 truncate" style={{ color: "var(--muted)" }}>{ex.description}</div>
                      </button>
                    );
                  })}
              </div>
              {expertSel && <p className="text-[11px] text-emerald-300">✓ 已选「{experts.find((e) => e.id === expertSel)?.name}」，可手动微调下方字段</p>}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2.5">
            <Field label="姓名" required error={agentForm.errors.name} hint="员工在组织里的称呼" htmlFor="ag-name">
              <input id="ag-name" placeholder="如：战略分析师" value={form.name}
                onChange={(e) => { setForm({ ...form, name: e.target.value }); agentForm.clear("name"); }}
                className="input" aria-invalid={!!agentForm.errors.name} />
            </Field>
            <Field label="岗位" error={agentForm.errors.role} hint="决定它的职责范围，可留空" htmlFor="ag-role">
              <input id="ag-role" placeholder="如：战略规划专家" value={form.role}
                onChange={(e) => { setForm({ ...form, role: e.target.value }); agentForm.clear("role"); }}
                className="input" aria-invalid={!!agentForm.errors.role} />
            </Field>
            <select value={form.department_id} onChange={(e) => setForm({ ...form, department_id: e.target.value })} className="input">
              <option value="">选择部门</option>
              {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
            </select>
            <select value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} className="input">
              <option value="">选择 AI 底座 / 大模型</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.name} ({m.provider_type} · {m.model_id})</option>
              ))}
            </select>
          </div>
          <Field label="人设与职责设定" error={agentForm.errors.system_prompt}
            hint="人格与职责设定；留空则使用该岗位的默认设定" htmlFor="ag-prompt">
            <textarea id="ag-prompt" placeholder="例如：你是一位谨慎的战略分析师，先给结论再给论据…" rows={2}
              value={form.system_prompt}
              onChange={(e) => { setForm({ ...form, system_prompt: e.target.value }); agentForm.clear("system_prompt"); }}
              className="input" aria-invalid={!!agentForm.errors.system_prompt} />
          </Field>

          {/* 📎 附件口：给新员工挂参考材料（制度、SOP、参考文档） */}
          <AttachmentPicker value={agentAttachments} onChange={setAgentAttachments}
            label="📎 挂参考材料（制度 / SOP / 培训文档）" />

          {/* 执行底座 */}
          <div>
            <p className="text-xs mb-1.5" style={{ color: "var(--muted)" }}>执行底座（由什么驱动这个员工）</p>
            <div className="grid grid-cols-3 gap-2">
              {backends.map((b) => (
                <button key={b.key} type="button"
                  onClick={() => setForm({ ...form, backend: b.key })}
                  className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                    form.backend === b.key
                      ? "border-indigo-500 bg-indigo-500/15"
                      : "hover:bg-white/[0.04]"}`}
                  style={form.backend === b.key ? {} : { borderColor: "var(--border)" }}>
                  <div className="font-medium">{b.label}</div>
                  <div className="mt-0.5 opacity-70" style={{ color: "var(--muted)" }}>{b.description}</div>
                </button>
              ))}
            </div>
          </div>

          {/* 能力包多选 */}
          <div>
            <p className="text-xs mb-1.5" style={{ color: "var(--muted)" }}>
              智能体能力包（像 Hermes / Codex 一样干活的能力，可多选）
            </p>
            <div className="grid grid-cols-3 gap-2">
              {capPacks.map((c) => {
                const on = selCaps.has(c.key);
                return (
                  <button key={c.key} type="button"
                    onClick={() => setSelCaps((prev) => {
                      const n = new Set(prev); if (n.has(c.key)) n.delete(c.key); else n.add(c.key); return n;
                    })}
                    className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                      on ? "border-emerald-500/60 bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}
                    style={on ? {} : { borderColor: "var(--border)" }}>
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{c.label}</span>
                      {c.risk === "high" && <span className="text-[9px] px-1 py-0.5 rounded bg-red-500/20 text-red-300">高危</span>}
                      {c.risk === "medium" && <span className="text-[9px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-300">中风险</span>}
                    </div>
                    <div className="mt-0.5 opacity-70" style={{ color: "var(--muted)" }}>{c.description}</div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* 不用 disabled：按钮点不动时用户不知道缺什么。
              保持可点击，点击后由校验给出"请填写姓名"这类明确原因。 */}
          <button onClick={createAgent} className="btn-primary">确认创建</button>
        </div>
      )}

      {/* 批量创建表单 */}
      {showBatchForm && (
        <div className="card mb-4 space-y-2.5">
          <div className="flex items-center justify-between">
            <h3 className="font-medium text-sm">⚡ 批量添加员工</h3>
            {/* V5.0 双模式切换 */}
            <div className="flex rounded-lg overflow-hidden border border-[var(--border)] text-xs">
              <button type="button" onClick={() => setBatchMode("text")}
                className={`px-3 py-1 transition-colors ${batchMode === "text" ? "bg-indigo-500/30 text-white" : "text-[var(--muted)] hover:text-white"}`}>
                ✍️ 手动输入
              </button>
              <button type="button" onClick={() => setBatchMode("experts")}
                className={`px-3 py-1 transition-colors ${batchMode === "experts" ? "bg-emerald-500/30 text-white" : "text-[var(--muted)] hover:text-white"}`}>
                📚 专家库勾选
              </button>
            </div>
          </div>

          {batchMode === "text" ? (
            <>
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                每行一名员工，格式：<code>姓名, 岗位</code>（岗位可省略）
              </p>
              <textarea rows={5} value={batchText} onChange={(e) => setBatchText(e.target.value)}
                placeholder={"市场总监, 市场负责人\n文案策划, 内容创作\n数据分析师"} className="input font-mono" />
            </>
          ) : (
            /* V5.0 专家库勾选：从 302 位专家里多选，一键批量生成数字员工 */
            <>
              <div className="flex items-center gap-2">
                <input value={expertSearch} onChange={(e) => setExpertSearch(e.target.value)}
                  placeholder={`搜索专家（${experts.length} 位）…`} className="input text-xs flex-1" />
                <button type="button" className="btn-ghost text-xs px-2 py-1"
                  onClick={() => setBatchExperts(new Set(batchExperts.size === experts.length ? [] : experts.map((e) => e.id)))}>
                  {batchExperts.size === experts.length ? "取消全选" : "全选"}
                </button>
                <span className="text-xs px-2 py-1 rounded-lg bg-emerald-500/15 text-emerald-300 border border-emerald-400/30">
                  已选 {batchExperts.size}
                </span>
              </div>
              <div className="grid grid-cols-3 lg:grid-cols-4 gap-1.5 max-h-52 overflow-y-auto pr-1">
                {experts
                  .filter((ex) => !expertSearch.trim() ||
                    ex.name.toLowerCase().includes(expertSearch.toLowerCase()) ||
                    (ex.description || "").toLowerCase().includes(expertSearch.toLowerCase()))
                  .slice(0, 60).map((ex) => {
                    const on = batchExperts.has(ex.id);
                    return (
                      <button key={ex.id} type="button"
                        onClick={() => setBatchExperts((prev) => {
                          const n = new Set(prev);
                          if (n.has(ex.id)) n.delete(ex.id); else n.add(ex.id);
                          return n;
                        })}
                        className={`text-left px-2.5 py-1.5 rounded-lg text-xs transition-all border ${
                          on ? "border-emerald-400/60 bg-emerald-500/15" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "var(--border)" }}>
                        <div className="font-medium flex items-center gap-1">
                          {on && <span className="text-emerald-300">✓</span>}
                          {ex.emoji || "🧑‍💼"} {ex.name}
                        </div>
                        <div className="text-[10px] opacity-60 truncate" style={{ color: "var(--muted)" }}>{ex.description}</div>
                      </button>
                    );
                  })}
              </div>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                勾选的专家会自动带出人设，创建为数字员工；统一部门/上级/模型见下方
              </p>
            </>
          )}

          <div className="grid grid-cols-3 gap-2.5">
            <select value={batchDept} onChange={(e) => setBatchDept(e.target.value)} className="input">
              <option value="">统一部门（可选）</option>
              {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
            </select>
            <select value={batchMgr} onChange={(e) => setBatchMgr(e.target.value)} className="input">
              <option value="">统一上级（可选）</option>
              {agents.map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
            </select>
            <select value={batchModel} onChange={(e) => setBatchModel(e.target.value)} className="input">
              <option value="">统一模型（可选）</option>
              {models.map((m) => (<option key={m.id} value={m.id}>{m.name}</option>))}
            </select>
          </div>
          <button onClick={batchCreate}
            disabled={batchMode === "text" ? !batchText.trim() : batchExperts.size === 0}
            className="btn-primary disabled:opacity-40">
            {batchMode === "experts" ? `批量创建 ${batchExperts.size} 名专家员工` : "批量创建"}
          </button>
        </div>
      )}

      {/* 浮动批量操作条：仅选中时出现 */}
      {selected.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2 px-4 py-2.5 rounded-2xl shadow-2xl animate-fade-up"
          style={{
            background: "linear-gradient(135deg, rgba(30,30,45,0.95), rgba(25,25,38,0.95))",
            border: "1px solid rgba(99,102,241,0.4)",
            backdropFilter: "blur(12px)",
          }}>
          <span className="text-sm px-1">已选 <b className="text-indigo-300">{selected.size}</b></span>
          <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />
          <button onClick={() => setSelected(new Set(shown.map((a) => a.id)))} className="btn-ghost text-xs">全选</button>
          <button onClick={batchResume} className="btn-ghost text-xs">▶ 恢复</button>
          <button onClick={batchPause} className="btn-ghost text-xs">⏸ 暂停</button>
          <button onClick={batchDelete}
            className="text-xs py-1.5 px-3 rounded-lg text-red-300 hover:bg-red-500/15 transition-colors"
            style={{ border: "1px solid rgba(248,113,113,0.35)" }}>
            🗑 删除
          </button>
          <button onClick={() => setSelected(new Set())} className="btn-ghost text-xs">✕</button>
        </div>
      )}

      {/* 组织架构视图：按部门分组 → 上级 → 缩进下属 */}
      <div className="space-y-6">
        {Object.entries(groupedRoots)
          .sort(([an], [bn]) => {
            const ai = pinnedDepts.indexOf(an), bi = pinnedDepts.indexOf(bn);
            if (ai !== -1 && bi === -1) return -1;
            if (bi !== -1 && ai === -1) return 1;
            if (ai !== -1 && bi !== -1) return ai - bi;
            return an.localeCompare(bn, "zh");
          })
          .map(([deptName, roots]) => {
          const collapsed = collapsedDepts.has(deptName);
          const sorted = [...roots].sort((a, b) => {
            const ai = pinnedAgents.indexOf(a.id), bi = pinnedAgents.indexOf(b.id);
            if (ai !== -1 && bi === -1) return -1;
            if (bi !== -1 && ai === -1) return 1;
            if (ai !== -1 && bi !== -1) return ai - bi;
            return a.name.localeCompare(b.name, "zh");
          });
          return (
            <div key={deptName}>
              <div className="flex items-center gap-2 mb-3 group/dept">
                <button
                  onClick={() => setCollapsedDepts((prev) => {
                    const n = new Set(prev);
                    if (n.has(deptName)) n.delete(deptName); else n.add(deptName);
                    return n;
                  })}
                  className="flex items-center gap-2 transition-colors"
                  style={{ color: "var(--foreground)" }}>
                  <span className={`text-sm text-indigo-300 transition-transform ${collapsed ? "-rotate-90" : ""}`}>▾</span>
                  <span className="text-lg font-bold tracking-wide group-hover/dept:text-indigo-200">
                    {pinnedDepts.includes(deptName) && <span className="text-xs mr-1">📌</span>}
                    {deptName}
                  </span>
                  <span className="text-xs opacity-60" style={{ color: "var(--muted)" }}>· {roots.length} 人</span>
                </button>
                <button onClick={() => togglePinDept(deptName)}
                  title={pinnedDepts.includes(deptName) ? "取消置顶" : "置顶该部门"}
                  className={`text-[11px] px-2 py-0.5 rounded-md transition-colors ${pinnedDepts.includes(deptName)
                    ? "text-amber-300 bg-amber-500/15" : "opacity-0 group-hover/dept:opacity-100 hover:bg-white/[0.06]"}`}
                  style={{ color: pinnedDepts.includes(deptName) ? undefined : "var(--muted)" }}>
                  📌 {pinnedDepts.includes(deptName) ? "已置顶" : "置顶"}
                </button>
              </div>
              {!collapsed && (
                <div className="grid grid-cols-4 gap-3">
                  {sorted.map((a) => renderCard(a))}
                </div>
              )}
            </div>
          );
        })}
        {!Object.keys(groupedRoots).length && (
          <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
            没有符合条件的 Agent
          </div>
        )}
      </div>

      {/* ---------- 编辑弹窗（Portal 渲染到 body，不受祖先 transform 限制）---------- */}
      {editing && mounted && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setEditing(null)}>
          <div className="card w-full max-w-3xl space-y-3 max-h-[90vh] overflow-y-auto"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)" }}
            onClick={(e) => e.stopPropagation()}>
            {/* 弹窗头 */}
            <div className="flex items-center justify-between pb-2 border-b" style={{ borderColor: "var(--border)" }}>
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-xl flex items-center justify-center text-white font-bold"
                  style={{ background: `linear-gradient(135deg, ${AVATAR_COLORS[agents.findIndex((x) => x.id === editing.id) % AVATAR_COLORS.length]})` }}>
                  {editing.name.slice(0, 1)}
                </div>
                <div>
                  <h3 className="font-bold">{editing.name}</h3>
                  <p className="text-xs" style={{ color: "var(--muted)" }}>编辑员工信息与能力配置</p>
                </div>
              </div>
              <button onClick={() => setEditing(null)} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <div>
                <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>岗位</label>
                <input value={editForm.role} onChange={(e) => setEditForm({ ...editForm, role: e.target.value })}
                  placeholder="如：战略规划专家" className="input" />
              </div>
              <div>
                <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>状态</label>
                <select value={editForm.status} onChange={(e) => setEditForm({ ...editForm, status: e.target.value })} className="input">
                  {["IDLE", "ACTIVE", "RUNNING", "PAUSED", "DISABLED", "ERROR"].map((s) => (
                    <option key={s} value={s}>{zh(AGENT_STATUS_ZH, s)}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🏢 部门归属</label>
                <select value={editForm.department_id}
                  onChange={(e) => setEditForm({ ...editForm, department_id: e.target.value })}
                  className="input">
                  <option value="">未分配部门</option>
                  {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
                </select>
              </div>
              <div>
                <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>↳ 直接上级</label>
                <select value={editForm.manager_agent_id}
                  onChange={(e) => setEditForm({ ...editForm, manager_agent_id: e.target.value })}
                  className="input">
                  <option value="">（无上级 · 顶层）</option>
                  {agents.filter((x) => x.id !== editing.id).map((x) => (
                    <option key={x.id} value={x.id}>{x.name}{x.role ? ` · ${x.role}` : ""}</option>
                  ))}
                </select>
              </div>
            </div>

            {/* 模型 */}
            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🧬 驱动大模型</label>
              <select value={editForm.model_id}
                onChange={(e) => setEditForm({ ...editForm, model_id: e.target.value })}
                className="input">
                <option value="">未绑定模型</option>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>{m.name} ({m.provider_type} · {m.model_id})</option>
                ))}
              </select>
              <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                更多供应商可在「智能体 → 模型配置」页添加
              </p>
            </div>

            {/* 执行底座 */}
            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🤖 执行底座（本地智能体赋能）</label>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {backends.map((b) => (
                  <button key={b.key} type="button"
                    onClick={() => setEditForm({ ...editForm, backend: b.key })}
                    className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                      editForm.backend === b.key
                        ? "border-indigo-500 bg-indigo-500/15"
                        : "hover:bg-white/[0.04]"}`}
                    style={editForm.backend === b.key ? {} : { borderColor: "var(--border)" }}>
                    <div className="font-medium">{b.label}</div>
                    <div className="mt-0.5 opacity-70 line-clamp-1" style={{ color: "var(--muted)" }}>{b.description}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* 能力包 */}
            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>⚡ 智能体能力包（可随时增减）</label>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {capPacks.map((c) => {
                  const on = editForm.capabilities.has(c.key);
                  return (
                    <button key={c.key} type="button"
                      onClick={() => setEditForm((prev) => {
                        const n = new Set(prev.capabilities);
                        if (n.has(c.key)) n.delete(c.key); else n.add(c.key);
                        return { ...prev, capabilities: n };
                      })}
                      className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                        on ? "border-emerald-500/60 bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}
                      style={on ? {} : { borderColor: "var(--border)" }}>
                      <div className="flex items-center justify-between">
                        <span>{c.label}</span>
                        {on && <span className="text-emerald-400">✓</span>}
                        {c.risk === "high" && !on && <span className="text-[9px] px-1 rounded bg-red-500/20 text-red-300">高危</span>}
                        {c.risk === "medium" && !on && <span className="text-[9px] px-1 rounded bg-amber-500/20 text-amber-300">中风险</span>}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* System Prompt */}
            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>📝 人设与职责设定</label>
              <textarea rows={2} value={editForm.system_prompt}
                onChange={(e) => setEditForm({ ...editForm, system_prompt: e.target.value })}
                placeholder="留空使用默认人格" className="input" />
            </div>

            {/* 操作区 */}
            <div className="flex items-center gap-2 pt-2 border-t" style={{ borderColor: "var(--border)" }}>
              <button onClick={saveEdit} disabled={saving} className="btn-primary disabled:opacity-40">
                {saving ? "保存中…" : "保存修改"}
              </button>
              <button onClick={() => router.push(`/agents/${editing.id}/profile`)} className="btn-ghost">
                查看完整档案 →
              </button>
              <div className="flex-1" />
              <button onClick={() => deleteOne(editing)}
                className="text-xs py-1.5 px-3 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10 transition-colors"
                style={{ border: "1px solid rgba(248,113,113,0.3)" }}>
                🗑 删除此员工
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
